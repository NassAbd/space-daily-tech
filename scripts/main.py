"""
Space Daily Tech — Main Pipeline
===============================
Flow: CNES RSS + Rêves d'Espace Scraping → Gemini 2.5 Flash (EN summary)
      → Gemini TTS (WAV) → data.json

Usage:
    uv run python scripts/main.py           # Production
    uv run python scripts/main.py --dry-run # Offline mode (local fixture)

Type check: uvx ty check scripts/main.py
Lint      : uvx ruff check scripts/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import feedparser
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVES_URL = "https://reves-d-espace.com/"

TEXT_MODEL = "gemini-2.5-flash"
TTS_MODEL = "gemini-2.5-flash-preview-tts"
AUDIO_PATH = Path("audio/latest_report.wav")
DATA_JSON_PATH = Path("data.json")
WINDOW_HOURS = 24
DEFAULT_VOICE = "Charon"

# ---------------------------------------------------------------------------
# Data Schema (Contract-First)
# ---------------------------------------------------------------------------


class ArticleItem(TypedDict):
    title: str
    summary: str
    link: str
    published: str
    source: str
    already_seen: bool


class DailyReport(TypedDict):
    date: str
    title: str
    summary: str
    article_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def get_cnes_rss_url() -> str:
    """Returns the CNES RSS URL, dynamically checking environment variables."""
    url = os.getenv("RSS_FEED_URL") or "https://cnes.fr/rss/actualites"
    if "techcrunch.com" in url:
        return "https://cnes.fr/rss/actualites"
    return url


def _struct_to_dt(struct_time: time.struct_time) -> datetime:
    """Converts a struct_time (feedparser) into an aware UTC datetime."""
    return datetime(*struct_time[:6], tzinfo=timezone.utc)


def _parse_iso_to_utc(iso_str: str) -> datetime:
    """Parses ISO-8601 string to an aware UTC datetime."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Source 1 — Rêves d'Espace Scraper
# ---------------------------------------------------------------------------


def _fetch_reves_article_summary(link: str) -> str:
    """
    Fetches the article detail page and extracts the text inside the wordpress entry-content class.
    Gets the first few paragraphs to use as a rich summary.
    """
    print(f"[Scraper] Fetching article detail: {link}")
    try:
        response = httpx.get(link, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"[Scraper] Error fetching article detail {link}: {e}")
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    content_div = soup.find("div", class_="entry-content")
    if not content_div:
        content_div = soup

    paragraphs = content_div.find_all("p")
    text_blocks: list[str] = []
    for p in paragraphs:
        text = p.get_text(strip=True)
        if text:
            text_blocks.append(text)
            if len(text_blocks) >= 3:
                break

    return " ".join(text_blocks)


def fetch_reves_recent(window_hours: int = WINDOW_HOURS) -> list[ArticleItem]:
    """
    Scrapes Rêves d'Espace homepage, extracts recent articles, and fetches their summaries.
    """
    print(f"[Scraper] Fetching Rêves d'Espace homepage: {REVES_URL}")
    try:
        response = httpx.get(REVES_URL, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"[Scraper] Error fetching Rêves d'Espace homepage: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    articles: list[ArticleItem] = []
    cutoff = _utcnow() - timedelta(hours=window_hours)

    for article_tag in soup.find_all("article"):
        title_tag = article_tag.find("h2", class_="cm-entry-title")
        if not title_tag:
            continue
        a_tag = title_tag.find("a")
        if not a_tag:
            continue
        link = a_tag.get("href")
        if not isinstance(link, str):
            continue

        title = " ".join(a_tag.get_text().split())

        time_tag = article_tag.find("time", class_="entry-date")
        if not time_tag:
            continue
        dt_attr = time_tag.get("datetime")
        if not isinstance(dt_attr, str):
            continue

        try:
            pub_dt = _parse_iso_to_utc(dt_attr)
        except ValueError:
            print(f"[Scraper] Error parsing datetime string: {dt_attr}")
            continue

        if pub_dt < cutoff:
            continue

        summary = _fetch_reves_article_summary(link)

        articles.append(
            ArticleItem(
                title=title,
                summary=summary,
                link=link,
                published=pub_dt.isoformat(),
                source="Rêves d'Espace",
                already_seen=False,
            )
        )

    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles


def fetch_reves_last_absolute() -> ArticleItem | None:
    """
    Scrapes the single absolute most recent article from Rêves d'Espace.
    """
    print(f"[Scraper] Fetching absolute latest from Rêves d'Espace: {REVES_URL}")
    try:
        response = httpx.get(REVES_URL, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"[Scraper] Error fetching Rêves d'Espace homepage: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    parsed_articles: list[tuple[datetime, str, str]] = []

    for article_tag in soup.find_all("article"):
        title_tag = article_tag.find("h2", class_="cm-entry-title")
        if not title_tag:
            continue
        a_tag = title_tag.find("a")
        if not a_tag:
            continue
        link = a_tag.get("href")
        if not isinstance(link, str):
            continue

        time_tag = article_tag.find("time", class_="entry-date")
        if not time_tag:
            continue
        dt_attr = time_tag.get("datetime")
        if not isinstance(dt_attr, str):
            continue

        try:
            pub_dt = _parse_iso_to_utc(dt_attr)
            parsed_articles.append((pub_dt, " ".join(a_tag.get_text().split()), link))
        except ValueError:
            continue

    if not parsed_articles:
        return None

    parsed_articles.sort(key=lambda x: x[0], reverse=True)
    latest_dt, latest_title, latest_link = parsed_articles[0]

    summary = _fetch_reves_article_summary(latest_link)

    return ArticleItem(
        title=latest_title,
        summary=summary,
        link=latest_link,
        published=latest_dt.isoformat(),
        source="Rêves d'Espace",
        already_seen=True,
    )


# ---------------------------------------------------------------------------
# Source 2 — CNES RSS Feed Parser
# ---------------------------------------------------------------------------


def fetch_cnes_recent(window_hours: int = WINDOW_HOURS) -> list[ArticleItem]:
    """
    Retrieves recent CNES articles from the RSS feed.
    """
    url = get_cnes_rss_url()
    print(f"[CNES RSS] Fetching feed: {url}")
    feed = feedparser.parse(url)

    if feed.bozo:
        print(f"[CNES RSS] Warning: feed parsed with errors — {feed.bozo_exception}")

    cutoff = _utcnow() - timedelta(hours=window_hours)
    articles: list[ArticleItem] = []

    for entry in feed.entries:
        if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
            continue
        published_dt = _struct_to_dt(entry.published_parsed)
        if published_dt < cutoff:
            continue

        raw_summary = entry.get("summary", entry.get("description", ""))
        clean_summary = BeautifulSoup(raw_summary, "html.parser").get_text(strip=True)

        articles.append(
            ArticleItem(
                title=entry.get("title", "No title"),
                summary=clean_summary,
                link=entry.get("link", ""),
                published=published_dt.isoformat(),
                source="CNES",
                already_seen=False,
            )
        )

    articles.sort(key=lambda a: a["published"], reverse=True)
    print(f"[CNES RSS] {len(articles)} article(s) found in the last {window_hours} hours.")
    return articles


def fetch_cnes_last_absolute() -> ArticleItem | None:
    """
    Retrieves the single absolute last article from CNES RSS feed.
    """
    url = get_cnes_rss_url()
    print(f"[CNES RSS] Fetching absolute latest from CNES feed: {url}")
    feed = feedparser.parse(url)

    if not feed.entries:
        return None

    entries_with_date = []
    for entry in feed.entries:
        if hasattr(entry, "published_parsed") and entry.published_parsed is not None:
            entries_with_date.append((_struct_to_dt(entry.published_parsed), entry))
        else:
            entries_with_date.append((datetime.min.replace(tzinfo=timezone.utc), entry))

    entries_with_date.sort(key=lambda x: x[0], reverse=True)
    latest_dt, entry = entries_with_date[0]

    raw_summary = entry.get("summary", entry.get("description", ""))
    clean_summary = BeautifulSoup(raw_summary, "html.parser").get_text(strip=True)

    return ArticleItem(
        title=entry.get("title", "No title"),
        summary=clean_summary,
        link=entry.get("link", ""),
        published=latest_dt.isoformat(),
        source="CNES",
        already_seen=True,
    )


# ---------------------------------------------------------------------------
# Aggregator & Fallback Logic
# ---------------------------------------------------------------------------


def fetch_all_sources(window_hours: int = WINDOW_HOURS) -> list[ArticleItem]:
    """
    Fetches articles from both CNES RSS and Rêves d'Espace scraping.
    If a source has no articles in the window, fallback grabs its absolute latest.
    """
    print(f"[Pipeline] Fetching all space tech sources (window={window_hours}h)...")
    articles: list[ArticleItem] = []

    cnes_list = fetch_cnes_recent(window_hours)
    if not cnes_list:
        print("[Pipeline] No CNES articles in the last 24h. Fetching absolute latest...")
        last_cnes = fetch_cnes_last_absolute()
        if last_cnes:
            articles.append(last_cnes)
    else:
        articles.extend(cnes_list)

    reves_list = fetch_reves_recent(window_hours)
    if not reves_list:
        print(
            "[Pipeline] No Rêves d'Espace articles in the last 24h. "
            "Fetching absolute latest..."
        )
        last_reves = fetch_reves_last_absolute()
        if last_reves:
            articles.append(last_reves)
    else:
        articles.extend(reves_list)

    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles


def _build_articles_text(articles: list[ArticleItem]) -> str:
    """Formats articles into a text block for the Gemini prompt."""
    lines: list[str] = []
    for i, art in enumerate(articles, start=1):
        lines.append(f"### Article {i} — {art['title']}")
        lines.append(f"Source: {art['source']}")
        lines.append(f"Published: {art['published']}")
        lines.append(f"Link: {art['link']}")
        lines.append(f"Already Seen: {art['already_seen']}")
        lines.append(art["summary"])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 2 — Summary & Prompting via Gemini Flash (English)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a professional, dynamic space technology radio presenter and expert news anchor.
Your mission is to write a captivating, fluent morning briefing in English based on
the articles provided.

The briefing must:
- Start with an engaging hook including the today's date provided.
- Present each news item in a concise, clear, and highly engaging manner
  (translating any French source content into fluent English).
- Use an oral journalistic style (short sentences, natural transitions, high-energy tone).
- End with a warm, professional closing remark.
- Be WRITTEN ENTIRELY IN ENGLISH.

CRITICAL INSTRUCTIONS:
- NEVER use placeholders or bracketed/parenthetical text to be filled in (e.g., NO [date], [name]).
  The script must be fully readable as is.
- Do not invent any facts; base your synthesis strictly on the provided articles.

FALLBACK / RE-RUN INSTRUCTIONS:
- If any article in the provided list is marked with 'already_seen: True', do NOT introduce
  it as breaking news. Instead, present it as a recap or highlight of recent space events
  (e.g., 'As we wait for today's new launches, let's look back at a major recent development...').
"""


def generate_briefing(client: genai.Client, articles: list[ArticleItem]) -> str:
    """
    Sends articles to Gemini 2.5 Flash and returns the briefing text in English.
    """
    today_str = _utcnow().strftime("%B %d, %Y")
    articles_text = _build_articles_text(articles)

    user_message = (
        f"Today's date: {today_str}\n\n"
        f"Here are {len(articles)} space technology articles:\n\n"
        f"{articles_text}\n\n"
        "Generate the radio morning briefing in English."
    )

    print(f"[LLM] Generating briefing with {TEXT_MODEL}...")
    response = client.models.generate_content(
        model=TEXT_MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.7,
            max_output_tokens=4096,
        ),
    )

    if response.text is None:
        raise ValueError("[LLM] Error: Gemini response contains no text.")
    briefing = response.text.strip()
    print(f"[LLM] Briefing generated ({len(briefing)} characters).")
    return briefing


# ---------------------------------------------------------------------------
# Step 3 — Speech Synthesis via Gemini TTS
# ---------------------------------------------------------------------------


def _save_wav(path: Path, pcm_data: bytes) -> None:
    """Saves raw PCM data into a WAV file (mono, 24kHz, 16-bit)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(24000)
        wf.writeframes(pcm_data)
    print(f"[TTS] Audio saved: {path} ({path.stat().st_size / 1024:.1f} KB)")


def generate_audio(
    client: genai.Client, briefing_text: str, voice_name: str = DEFAULT_VOICE
) -> None:
    """
    Transforms the briefing text into WAV audio via Gemini TTS.
    """
    print(f"[TTS] Speech synthesis with {TTS_MODEL} (voice: {voice_name})...")
    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=briefing_text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name,
                    )
                )
            ),
        ),
    )

    candidates = response.candidates
    assert candidates, "[TTS] Error: no candidate in the TTS response."

    content = candidates[0].content
    if content is None or content.parts is None:
        raise ValueError("[TTS] Error: missing content or parts in the TTS response.")

    parts = content.parts
    if not parts or parts[0].inline_data is None:
        raise ValueError("[TTS] Error: no inline data in the TTS response.")

    pcm_data = parts[0].inline_data.data
    if pcm_data is None:
        raise ValueError("[TTS] Error: empty audio data (bytes).")

    _save_wav(AUDIO_PATH, pcm_data)


# ---------------------------------------------------------------------------
# Step 4 — Export data.json
# ---------------------------------------------------------------------------


def write_data_json(articles: list[ArticleItem], briefing_text: str) -> DailyReport:
    """
    Generates and writes `data.json` with the metadata for the day's briefing.
    """
    now = _utcnow()
    day_en = now.strftime("%B %d, %Y")

    topic = os.getenv("TOPIC_NAME") or "Space Tech"
    report: DailyReport = {
        "date": now.isoformat(),
        "title": f"{topic} Briefing — {day_en}",
        "summary": briefing_text,
        "article_count": len(articles),
    }

    DATA_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[JSON] data.json updated ({report['article_count']} articles).")
    return report


# ---------------------------------------------------------------------------
# Offline Fixture (--dry-run)
# ---------------------------------------------------------------------------

DRY_RUN_ARTICLES: list[ArticleItem] = [
    ArticleItem(
        title="NASA's SLS rocket ready for launch to the Moon",
        summary=(
            "NASA has finalized flight checks for the Space Launch System, "
            "preparing for a Lunar orbit mission."
        ),
        link="https://cnes.fr/dry-run-1",
        published=_utcnow().isoformat(),
        source="CNES",
        already_seen=False,
    ),
    ArticleItem(
        title="New supersonic rotors designed for Mars flight",
        summary=(
            "Engineers at JPL have tested ultra-high speed rotors for "
            "future helicopter exploration on Mars."
        ),
        link="https://reves-d-espace.com/dry-run-2",
        published=_utcnow().isoformat(),
        source="Rêves d'Espace",
        already_seen=False,
    ),
]


def dry_run() -> None:
    """Offline mode: generates a test data.json without calling the API."""
    print("[DRY-RUN] Offline mode enabled — no API calls.")
    fake_briefing = (
        "Good morning and welcome to your Space Daily Tech briefing! "
        "Today, we look at NASA's SLS lunar flight preparation and exciting "
        "new supersonic rotor designs from JPL for Martian flight. Have a wonderful day!"
    )
    report = write_data_json(DRY_RUN_ARTICLES, fake_briefing)
    print(f"[DRY-RUN] Fictional report generated: {report['title']}")
    print("[DRY-RUN] Note: no audio file produced in --dry-run mode.")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Space Daily Tech pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Offline mode: generates a fictional data.json without calling the Gemini API.",
    )
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        return

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[ERROR] GEMINI_API_KEY environment variable is missing.", file=sys.stderr)
        sys.exit(1)

    voice_name = os.getenv("TTS_VOICE_NAME") or DEFAULT_VOICE
    max_articles = int(os.getenv("MAX_ARTICLES") or "10")

    client = genai.Client(api_key=api_key)

    # --- Step 1: Scrape & Parse ---
    articles = fetch_all_sources(WINDOW_HOURS)

    if not articles:
        print("[WARN] No articles found across space sources. Pipeline stopped.")
        sys.exit(0)

    articles = articles[:max_articles]

    # --- Step 2: Text Briefing (English) ---
    briefing_text = generate_briefing(client, articles)

    # --- Step 3: Speech Synthesis ---
    generate_audio(client, briefing_text, voice_name=voice_name)

    # --- Step 4: JSON Export ---
    write_data_json(articles, briefing_text)

    print("[OK] Space Daily Tech pipeline completed successfully.")


if __name__ == "__main__":
    main()
