"""SEO basics — crawl-based, oracle-free, cheap (runs alongside the existing crawl).

Per-page rules (title, meta description, h1, heading order, canonical) plus
crawl-level rules (duplicate titles/descriptions, robots/sitemap presence).

Missing-alt is intentionally NOT reported here when the accessibility check is on —
axe already owns it (``image-alt``). We only emit alt findings if accessibility is
disabled, so the two checks never double-report.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse, urlunparse

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..findings import Finding, Severity

CHECK = "seo"
_EVAL_TIMEOUT = 10.0
_TITLE_MAX = 60  # chars — beyond this, search results truncate the title

_PAGE_JS = """
() => {
  const md = document.querySelector('meta[name="description"]');
  return {
    title: (document.title || '').trim(),
    metaDescription: md ? (md.getAttribute('content') || '').trim() : null,
    h1: document.querySelectorAll('h1').length,
    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
                   .map(h => parseInt(h.tagName[1], 10)),
    canonical: !!document.querySelector('link[rel="canonical"]'),
    imgsNoAlt: Array.from(document.images).filter(i => !i.hasAttribute('alt')).length,
  };
}
"""


class SeoCollector:
    """Per-page SEO rules + accumulation for crawl-level duplicate detection."""

    def __init__(self, *, report_alt: bool) -> None:
        # report_alt: only emit missing-alt when accessibility isn't running (dedup).
        self.report_alt = report_alt
        self.titles: dict[str, list[str]] = {}  # title -> [urls]
        self.descriptions: dict[str, list[str]] = {}

    async def check_page(self, page: Page, url: str) -> list[Finding]:
        try:
            data = await asyncio.wait_for(page.evaluate(_PAGE_JS), timeout=_EVAL_TIMEOUT)
        except (PlaywrightError, TimeoutError):
            return []

        findings: list[Finding] = []

        def add(type_, sev, title, detail, key):
            findings.append(Finding.create(check=CHECK, type=type_, severity=sev,
                                            title=title, detail=detail, page_url=url, key=key))

        title = data.get("title") or ""
        if not title:
            add("seo_title_missing", Severity.WARNING, "Missing <title>",
                "Page has no <title> element.", "title")
        else:
            self.titles.setdefault(title, []).append(url)
            if len(title) > _TITLE_MAX:
                add("seo_title_long", Severity.INFO, "Title too long",
                    f"Title is {len(title)} chars (>{_TITLE_MAX}); may be truncated.", "title_long")

        desc = data.get("metaDescription")
        if not desc:
            add("seo_meta_description_missing", Severity.MINOR, "Missing meta description",
                "Page has no <meta name=\"description\">.", "meta_desc")
        else:
            self.descriptions.setdefault(desc, []).append(url)

        h1 = data.get("h1", 0)
        if h1 == 0:
            add("seo_h1_missing", Severity.WARNING, "No H1 heading",
                "Page has no <h1>.", "h1_missing")
        elif h1 > 1:
            add("seo_h1_multiple", Severity.MINOR, "Multiple H1 headings",
                f"Page has {h1} <h1> elements; expected one.", "h1_multiple")

        # Heading order: a downward jump of more than one level (e.g. h1 -> h3).
        prev = 0
        for level in data.get("headings", []):
            if prev and level - prev > 1:
                add("seo_heading_order", Severity.MINOR, "Skipped heading level",
                    f"Heading jumps from h{prev} to h{level}; keep levels sequential.",
                    "heading_order")
                break
            prev = level

        if not data.get("canonical"):
            add("seo_canonical_missing", Severity.INFO, "No canonical tag",
                "Page has no <link rel=\"canonical\">.", "canonical")

        if self.report_alt and data.get("imgsNoAlt", 0) > 0:
            n = data["imgsNoAlt"]
            add("seo_img_alt_missing", Severity.MINOR, "Images missing alt text",
                f"{n} image(s) have no alt attribute.", "img_alt")

        return findings

    async def finalize(self, seed: str) -> list[Finding]:
        """Crawl-level rules: duplicate titles/descriptions + robots/sitemap presence."""
        findings: list[Finding] = []
        for title, urls in self.titles.items():
            if len(urls) > 1:
                findings.append(Finding.create(
                    check=CHECK, type="seo_title_duplicate", severity=Severity.MINOR,
                    title="Duplicate <title>",
                    detail=f"{len(urls)} pages share the title {title!r}.",
                    page_url=urls[0], key=f"dup_title|{title}"))
        for desc, urls in self.descriptions.items():
            if len(urls) > 1:
                findings.append(Finding.create(
                    check=CHECK, type="seo_meta_duplicate", severity=Severity.MINOR,
                    title="Duplicate meta description",
                    detail=f"{len(urls)} pages share the same meta description.",
                    page_url=urls[0], key=f"dup_desc|{desc[:80]}"))
        findings.extend(await self._site_files(seed))
        return findings

    async def _site_files(self, seed: str) -> list[Finding]:
        parts = urlparse(seed)
        if parts.scheme not in ("http", "https"):
            return []
        findings: list[Finding] = []
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for path, label in (("/robots.txt", "robots.txt"), ("/sitemap.xml", "sitemap.xml")):
                file_url = urlunparse((parts.scheme, parts.netloc, path, "", "", ""))
                try:
                    resp = await client.get(file_url)
                    present = resp.status_code == 200
                except httpx.HTTPError:
                    present = False
                if not present:
                    findings.append(Finding.create(
                        check=CHECK, type="seo_sitefile_missing", severity=Severity.INFO,
                        title=f"No {label}", detail=f"{file_url} not found (HTTP check).",
                        page_url=seed, key=path))
        return findings
