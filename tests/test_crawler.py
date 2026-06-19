"""Crawler bounds and resilience (criteria 2 and 5)."""

from __future__ import annotations

from qascan.config import RunLimits
from qascan.crawler import crawl, normalize_url, registrable_domain


async def test_max_pages_enforced(http_server):
    result = await crawl(f"{http_server}/index.html", RunLimits(max_pages=3, max_depth=3))
    assert result.pages_scanned == 3
    assert result.stopped_reason == "max_pages"


async def test_max_depth_enforced(http_server):
    # depth 0 => only the seed is scanned, no children enqueued.
    result = await crawl(f"{http_server}/index.html", RunLimits(max_pages=50, max_depth=0))
    assert result.pages_scanned == 1
    assert result.stopped_reason == "completed"


async def test_bad_page_is_critical_and_scan_continues(http_server):
    result = await crawl(
        f"{http_server}/crash_hub.html", RunLimits(max_pages=50, max_depth=1)
    )
    criticals = [f for f in result.findings if f.severity.value == "critical"]
    assert criticals, "the 500 page should produce a critical finding"
    assert any(f.type == "page_error" for f in criticals)
    # The healthy siblings were still scanned despite the bad page.
    assert result.pages_scanned >= 3
    assert result.stopped_reason == "completed"


def test_registrable_domain():
    assert registrable_domain("www.saucedemo.com") == "saucedemo.com"
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert registrable_domain("localhost") == "localhost"
    assert registrable_domain("127.0.0.1") == "127.0.0.1"


def test_normalize_url_dedupes_trailing_slash_and_fragment():
    assert normalize_url("https://x.com/a/") == normalize_url("https://x.com/a")
    assert normalize_url("https://x.com/a#frag") == "https://x.com/a"
    assert normalize_url("https://X.COM/a") == "https://x.com/a"
