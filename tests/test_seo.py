"""Phase 6 SEO check — rules fire, and missing-alt dedups against accessibility
(criterion 4)."""

from __future__ import annotations

from qascan.config import RunLimits
from qascan.crawler import crawl


async def test_seo_rules_fire(http_server):
    result = await crawl(f"{http_server}/seo_bad.html",
                         RunLimits(max_pages=1, max_depth=0), checks={"seo"})
    types = {f.type for f in result.findings}
    assert "seo_meta_description_missing" in types
    assert "seo_h1_multiple" in types
    assert "seo_heading_order" in types
    assert "seo_canonical_missing" in types
    assert "seo_title_long" in types
    # robots.txt / sitemap.xml don't exist on the fixture server.
    assert "seo_sitefile_missing" in types


async def test_seo_reports_alt_only_when_accessibility_off(http_server):
    # SEO alone -> it owns missing-alt.
    seo_only = await crawl(f"{http_server}/seo_bad.html",
                           RunLimits(max_pages=1, max_depth=0), checks={"seo"})
    assert any(f.type == "seo_img_alt_missing" for f in seo_only.findings)
    assert not any(f.type == "wcag_violation" for f in seo_only.findings)


async def test_seo_does_not_duplicate_accessibility_alt(http_server):
    # Both on -> axe owns alt (image-alt), SEO must NOT also report it.
    both = await crawl(f"{http_server}/seo_bad.html",
                       RunLimits(max_pages=1, max_depth=0),
                       checks={"seo", "accessibility"})
    seo_alt = [f for f in both.findings if f.type == "seo_img_alt_missing"]
    axe_alt = [f for f in both.findings if f.type == "wcag_violation"
               and "image-alt" in f.detail]
    assert seo_alt == []           # SEO defers to accessibility -> no double-report
    assert len(axe_alt) >= 1       # axe still catches it


async def test_default_scan_includes_seo_not_performance(http_server):
    # SEO is cheap -> default on; performance is heavy -> default off.
    result = await crawl(f"{http_server}/seo_bad.html", RunLimits(max_pages=1, max_depth=0))
    checks_present = {f.check for f in result.findings}
    assert "seo" in checks_present
    assert "performance" not in checks_present
