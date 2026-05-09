import os
from scripts.main import get_cnes_rss_url


def test_rss_url_config_via_env():
    """
    Checks if the pipeline uses the RSS_FEED_URL environment variable.
    """
    mock_url = "https://example.com/custom-feed/"
    os.environ["RSS_FEED_URL"] = mock_url
    try:
        assert get_cnes_rss_url() == mock_url
    finally:
        if "RSS_FEED_URL" in os.environ:
            del os.environ["RSS_FEED_URL"]


def test_rss_url_default():
    """
    Checks if the default RSS URL is used when no env var is set.
    """
    if "RSS_FEED_URL" in os.environ:
        del os.environ["RSS_FEED_URL"]
    assert get_cnes_rss_url() == "https://cnes.fr/rss/actualites"
