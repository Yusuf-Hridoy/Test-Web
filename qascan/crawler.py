"""Async BFS crawler — hard-bounded by pages, depth, and a time budget.

Same-registrable-domain only. robots.txt is honored for crawling. A single bad
page becomes a ``critical`` finding and never aborts the loop. Hardened against
query-param explosion, redirect duplicates, non-HTML resources, and SPA timing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .checks import accessibility, exploratory
from .checks.exploratory import ConsoleCollector, LinkChecker
from .checks.seo import SeoCollector
from .config import RunLimits
from .findings import Finding, Severity
from .urls import (
    looks_non_html,
    normalize_url,
    path_key,
    registrable_domain,
    same_registrable_domain,
)

log = logging.getLogger("qascan.crawler")

StoppedReason = str  # "completed" | "max_pages" | "time_budget"

# Cap on distinct query-variants crawled per path (calendars/facets/pagination).
_MAX_PARAM_VARIANTS_PER_PATH = 5

# Cheap, no-extra-deps checks default ON; heavy checks (performance) are opt-in.
DEFAULT_CHECKS = frozenset({"exploratory", "accessibility", "seo"})


@dataclass
class CrawlResult:
    findings: list[Finding] = field(default_factory=list)
    pages_scanned: int = 0
    stopped_reason: StoppedReason = "completed"
    duration_seconds: float = 0.0


# Re-exported for backwards compatibility with earlier imports/tests.
__all__ = ["CrawlResult", "crawl", "normalize_url", "registrable_domain"]


def _same_domain(seed: str, candidate: str) -> bool:
    return same_registrable_domain(seed, candidate)


async def _load_robots(seed: str) -> urllib.robotparser.RobotFileParser:
    """Fetch and parse robots.txt once. On any failure, allow everything."""
    rp = urllib.robotparser.RobotFileParser()
    parts = urlparse(seed)
    if parts.scheme not in ("http", "https"):
        rp.parse([])
        return rp
    robots_url = urlunparse((parts.scheme, parts.netloc, "/robots.txt", "", "", ""))
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(robots_url)
        rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except httpx.HTTPError:
        rp.parse([])
    return rp


async def crawl(
    seed: str,
    limits: RunLimits,
    checks: set[str] | None = None,
    *,
    on_progress=None,
    cancel=None,
) -> CrawlResult:
    """Crawl ``seed`` within ``limits`` and return findings + run metadata.

    ``checks`` selects which check types run (default: exploratory + accessibility
    + seo). 'performance' is heavy and opt-in; it runs once on the seed after crawl.

    ``on_progress`` (optional) is called with ``{"pages", "current"}`` after each page
    so a UI can show live progress. ``cancel`` (optional ``threading.Event``) stops the
    crawl cleanly at the next page boundary (browser is still closed; no orphan).
    """
    enabled = set(checks) if checks is not None else set(DEFAULT_CHECKS)
    seed = normalize_url(seed)
    start = time.monotonic()
    result = CrawlResult()

    robots = await _load_robots(seed)
    user_agent = "qascan"

    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    visited: set[str] = set()
    link_sources: dict[str, str] = {}
    param_variants: dict[str, int] = defaultdict(int)
    link_checker = LinkChecker(limits)
    # SEO dedups missing-alt against accessibility: only report alt if axe is off.
    seo = SeoCollector(report_alt="accessibility" not in enabled) if "seo" in enabled else None

    def deadline_hit() -> bool:
        return (time.monotonic() - start) >= limits.time_budget_seconds

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        collector = ConsoleCollector()
        collector.attach(page)

        try:
            while queue:
                if len(visited) >= limits.max_pages:
                    result.stopped_reason = "max_pages"
                    break
                if deadline_hit():
                    result.stopped_reason = "time_budget"
                    break
                if cancel is not None and cancel.is_set():
                    result.stopped_reason = "cancelled"
                    break

                url, depth = queue.popleft()
                if url in visited:
                    continue
                if url != seed and not _same_domain(seed, url):
                    continue
                if url.startswith(("http://", "https://")) and not robots.can_fetch(
                    user_agent, url
                ):
                    continue
                visited.add(url)
                log.info("crawl page=%s depth=%d visited=%d", url, depth, len(visited))

                page_findings = await _scan_page(
                    page, collector, url, depth, limits, queue, visited,
                    link_sources, param_variants, enabled, seo,
                )
                result.findings.extend(page_findings)
                if on_progress is not None:
                    try:
                        on_progress({"pages": len(visited), "current": url})
                    except Exception:  # noqa: BLE001 — progress must never break a scan
                        pass
                await asyncio.sleep(limits.polite_delay_seconds)
        finally:
            await context.close()
            await browser.close()

    if "exploratory" in enabled:
        result.findings.extend(await link_checker.check(link_sources))
    if seo is not None:
        result.findings.extend(await seo.finalize(seed))
    if "performance" in enabled:
        from .checks import performance
        result.findings.extend(await performance.run(seed))

    result.pages_scanned = len(visited)
    result.duration_seconds = round(time.monotonic() - start, 3)
    log.info("crawl done pages=%d findings=%d stopped=%s",
             result.pages_scanned, len(result.findings), result.stopped_reason)
    return result


def _content_type(response) -> str:
    try:
        return (response.headers or {}).get("content-type", "").lower()
    except Exception:  # noqa: BLE001
        return ""


async def _scan_page(
    page, collector: ConsoleCollector, url: str, depth: int, limits: RunLimits,
    queue: deque[tuple[str, int]], visited: set[str], link_sources: dict[str, str],
    param_variants: dict[str, int], enabled: set[str], seo: SeoCollector | None,
) -> list[Finding]:
    """Scan one page. Catches everything — a bad page is a finding, not a crash."""
    findings: list[Finding] = []
    collector.reset()

    # Non-HTML by extension (PDF/zip/image/etc.): record and don't navigate
    # (navigating a downloadable triggers a download error, not a page).
    if looks_non_html(url):
        return [Finding.create(
            check="exploratory", type="non_html", severity=Severity.INFO,
            title="Non-HTML resource",
            detail=f"{url} looks like a non-HTML file; skipped HTML checks.",
            page_url=url, key="ext",
        )]

    try:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=limits.nav_timeout_ms
        )
    except PlaywrightError as exc:
        msg = str(exc).splitlines()[0] if str(exc) else "navigation failed"
        return [Finding.create(
            check="exploratory", type="page_error", severity=Severity.CRITICAL,
            title="Page failed to load", detail=f"{url} could not be loaded: {msg}",
            page_url=url, key="nav_error",
        )]

    # Redirect dedupe: if we landed somewhere already scanned, don't double-count.
    final = normalize_url(page.url)
    if final != url:
        if final in visited:
            return []
        visited.add(final)

    # Non-HTML resources (PDF/zip/images): record and skip HTML-only checks.
    ctype = _content_type(response)
    if ctype and "html" not in ctype and "xml" not in ctype:
        return [Finding.create(
            check="exploratory", type="non_html", severity=Severity.INFO,
            title="Non-HTML resource", detail=f"{url} is {ctype}; skipped HTML checks.",
            page_url=url, key=ctype,
        )]

    # SPA settle: give the app a bounded chance to render/load (best-effort).
    for state in ("load", "networkidle"):
        try:
            await page.wait_for_load_state(state, timeout=4_000)
        except PlaywrightError:
            pass

    if "exploratory" in enabled:
        findings.extend(exploratory.check_page_status(response, url))
        findings.extend(exploratory.check_console(collector, url))
        findings.extend(await exploratory.check_broken_images(page, url))
    if "accessibility" in enabled:
        findings.extend(await accessibility.run_axe(page, url))
    if seo is not None:
        findings.extend(await seo.check_page(page, url))

    links = await exploratory.extract_links(page)
    for link in links:
        norm = normalize_url(link)
        link_sources.setdefault(norm, url)
        if depth >= limits.max_depth or not _same_domain(url, norm) or norm in visited:
            continue
        if looks_non_html(norm):  # liveness-checked above, but never crawled into
            continue
        # Cap query-param explosion: only a few variants per path.
        pk = path_key(norm)
        if urlparse(norm).query:
            if param_variants[pk] >= _MAX_PARAM_VARIANTS_PER_PATH:
                continue
            param_variants[pk] += 1
        queue.append((norm, depth + 1))

    return findings
