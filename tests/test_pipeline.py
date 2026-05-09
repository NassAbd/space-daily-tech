import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
import feedparser

# Ensure we can import from scripts
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.main import (
    ArticleItem,
    _utcnow,
)

# Paths to the raw fixture files provided by the user
CNES_XML_FIXTURE_PATH = Path("doc_new_sources/cnes_rss.xml")
REVES_HTML_FIXTURE_PATH = Path("doc_new_sources/reves_despace.html")


def test_utcnow():
    """Verify that _utcnow returns an aware UTC datetime."""
    now = _utcnow()
    assert now.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Mocks and Scraping Tests
# ---------------------------------------------------------------------------

@patch("httpx.get")
def test_fetch_reves_recent_success(mock_get):
    """
    Test that we can parse Rêves d'Espace articles from HTML,
    extract title, link, published date, and perform secondary content fetch.
    """
    # Read local HTML fixture for home page
    html_content = REVES_HTML_FIXTURE_PATH.read_text(encoding="utf-8")
    
    # Configure the mock GET call dynamically based on URL requested
    def mock_get_impl(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "rotors-supersoniques-sur-mars" in url:
            resp.text = (
                "<html><body><div class='entry-content'><p>First paragraph of Mars rotor article.</p>"
                "<p>Second paragraph.</p></div></body></html>"
            )
        else:
            resp.text = html_content
        return resp
        
    mock_get.side_effect = mock_get_impl
    
    # Import scraping function
    from scripts.main import fetch_reves_recent
    
    # We want a window wide enough to capture our fixture dates (e.g. May 2026)
    # Matched to the fixture date: 2026-05-09T12:00:00+00:00
    controlled_now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    with patch("scripts.main._utcnow", return_value=controlled_now):
        articles = fetch_reves_recent(window_hours=48)
        
        # Verify that we parsed and found the most recent article
        assert len(articles) > 0
        latest = articles[0]
        assert latest["title"] == "Rotors supersoniques sur Mars : vers une nouvelle génération d’hélicoptères d’exploration"
        assert "https://reves-d-espace.com/rotors-supersoniques-sur-mars" in latest["link"]
        assert latest["source"] == "Rêves d'Espace"
        assert latest["already_seen"] is False
        assert latest["summary"] == "First paragraph of Mars rotor article. Second paragraph."


def test_fetch_cnes_recent_success():
    """
    Test that we can parse CNES articles from the XML RSS feed
    and correctly filter them by the window_hours parameter.
    """
    # Import the function to be implemented
    from scripts.main import fetch_cnes_recent
    
    # Mocking feedparser.parse to return the parsed content of cnes_rss.xml
    feed_content = feedparser.parse(str(CNES_XML_FIXTURE_PATH))
    
    # May 7th, 2026 is when "Plato" was published in the feed.
    # We'll set the mocked now to May 8th, 2026 to verify 24-hour filtering
    controlled_now = datetime(2026, 5, 8, 6, 0, 0, tzinfo=timezone.utc)
    
    with patch("feedparser.parse", return_value=feed_content), \
         patch("scripts.main._utcnow", return_value=controlled_now):
         
         # Within 24 hours, "Plato" should be found
         articles_24h = fetch_cnes_recent(window_hours=24)
         assert len(articles_24h) == 1
         assert "Plato" in articles_24h[0]["title"]
         assert articles_24h[0]["source"] == "CNES"
         assert articles_24h[0]["already_seen"] is False


# ---------------------------------------------------------------------------
# Fallback / Slow Day Tests
# ---------------------------------------------------------------------------

@patch("httpx.get")
def test_fallback_logic_when_no_recent_articles(mock_get):
    """
    Verify that when no articles are found in the 24-hour window,
    the pipeline falls back to fetching the absolute last article of both sources
    and tags them with already_seen=True.
    """
    from scripts.main import fetch_all_sources
    
    # Read fixtures
    html_content = REVES_HTML_FIXTURE_PATH.read_text(encoding="utf-8")
    feed_content = feedparser.parse(str(CNES_XML_FIXTURE_PATH))
    
    # Configure the mock GET call dynamically based on URL requested
    def mock_get_impl(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "rotors-supersoniques-sur-mars" in url:
            resp.text = "<html><body><div class='entry-content'><p>Fallback body</p></div></body></html>"
        else:
            resp.text = html_content
        return resp
        
    mock_get.side_effect = mock_get_impl
    
    # Set the controlled current time to way in the future (e.g., year 2027) so 24h filter returns nothing
    future_now = datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    
    with patch("feedparser.parse", return_value=feed_content), \
         patch("scripts.main._utcnow", return_value=future_now):
         
         articles = fetch_all_sources(window_hours=24)
         
         # Fallback should have returned exactly 2 articles (one from CNES, one from Rêves d'Espace)
         assert len(articles) == 2
         
         # Both should be marked as already seen
         assert articles[0]["already_seen"] is True
         assert articles[1]["already_seen"] is True
         
         # Ensure we got one of each source
         sources = {a["source"] for a in articles}
         assert sources == {"CNES", "Rêves d'Espace"}
