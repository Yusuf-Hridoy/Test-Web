"""Exploratory checks against the broken fixture (criterion 3)."""

from __future__ import annotations

from qascan.checks import exploratory
from qascan.config import RunLimits


async def _load(page, url):
    coll = exploratory.ConsoleCollector()
    coll.attach(page)
    coll.reset()
    resp = await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")
    return coll, resp


async def test_broken_image_detected(page, http_server):
    coll, _ = await _load(page, f"{http_server}/broken.html")
    findings = await exploratory.check_broken_images(page, f"{http_server}/broken.html")
    assert findings, "expected at least one broken image"
    assert all(f.type == "broken_image" and f.severity.value == "warning" for f in findings)


async def test_console_error_detected(page, http_server):
    coll, _ = await _load(page, f"{http_server}/broken.html")
    findings = exploratory.check_console(coll, f"{http_server}/broken.html")
    assert any("fixture console error" in f.detail for f in findings)
    assert all(f.type == "console_error" for f in findings)


async def test_broken_link_detected(page, http_server):
    coll, _ = await _load(page, f"{http_server}/broken.html")
    links = await exploratory.extract_links(page)
    sources = {link: f"{http_server}/broken.html" for link in links}
    checker = exploratory.LinkChecker(RunLimits())
    findings = await checker.check(sources)
    assert any("this-page-does-not-exist" in f.detail for f in findings)
    assert all(f.type == "broken_link" and f.severity.value == "warning" for f in findings)


async def test_page_status_404_is_critical(page, http_server):
    url = f"{http_server}/no-such-file.html"
    resp = await page.goto(url, wait_until="domcontentloaded")
    findings = exploratory.check_page_status(resp, url)
    assert len(findings) == 1
    assert findings[0].severity.value == "critical"
    assert findings[0].type == "page_error"
