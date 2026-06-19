"""Async BFS crawler — hard-bounded by pages, depth, and a time budget.

Same-registrable-domain only. robots.txt is honored for crawling. A single bad
page becomes a ``critical`` finding and never aborts the loop.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urlparse, urlunparse

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .checks import accessibility, exploratory
from .checks.exploratory import ConsoleCollector, LinkChecker
from .config import RunLimits
from .findings import Finding, Severity

# Public suffixes that have a meaningful third label (registrable = last 3).
# Small curated list — full PSL is out of scope for Phase 1 deps.
_TWO_LEVEL_SUFFIXES = {
    ("co", "uk"), ("org", "uk"), ("gov", "uk"), ("ac", "uk"), ("me", "uk"),
    ("com", "au"), ("net", "au"), ("org", "au"), ("co", "nz"), ("co", "jp"),
    ("com", "br"), ("co", "in"), ("co", "za"),
}
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

StoppedReason = str  # one of: "completed" | "max_pages" | "time_budget"


@dataclass
class CrawlResult:
    findings: list[Finding] = field(default_factory=list)
    pages_scanned: int = 0
    stopped_reason: StoppedReason = "completed"
    duration_seconds: float = 0.0


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1. IPs and ``localhost`` return as-is."""
    host = host.lower().strip(".")
    if not host or host == "localhost" or _IPV4_RE.match(host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if tuple(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_url(url: str) -> str:
    """Canonical form for dedupe: drop fragment, lowercase scheme/host, strip a
    trailing slash from non-root paths."""
    url, _ = urldefrag(url)
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, parts.params, parts.query, ""))


def _same_domain(seed: str, candidate: str) -> bool:
    s, c = urlparse(seed), urlparse(candidate)
    if s.scheme == "file" or c.scheme == "file":
        return s.scheme == c.scheme == "file"
    if c.scheme not in ("http", "https"):
        return False
    return registrable_domain(s.hostname or "") == registrable_domain(c.hostname or "")


async def _load_robots(seed: str) -> urllib.robotparser.RobotFileParser:
    """Fetch and parse robots.txt once. On any failure, allow everything."""
    rp = urllib.robotparser.RobotFileParser()
    parts = urlparse(seed)
    if parts.scheme not in ("http", "https"):
        rp.parse([])  # file:// etc. — no robots, allow all
        return rp
    robots_url = urlunparse((parts.scheme, parts.netloc, "/robots.txt", "", "", ""))
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(robots_url)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp.parse([])
    except httpx.HTTPError:
        rp.parse([])
    return rp


async def crawl(seed: str, limits: RunLimits) -> CrawlResult:
    """Crawl ``seed`` within ``limits`` and return findings + run metadata."""
    seed = normalize_url(seed)
    start = time.monotonic()
    result = CrawlResult()

    robots = await _load_robots(seed)
    user_agent = "qascan"

    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    visited: set[str] = set()
    link_sources: dict[str, str] = {}  # url -> page where first seen
    link_checker = LinkChecker(limits)

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

                page_findings = await _scan_page(
                    page, collector, url, depth, limits, queue, visited, link_sources
                )
                result.findings.extend(page_findings)

                await asyncio.sleep(limits.polite_delay_seconds)
        finally:
            await context.close()
            await browser.close()

    # Link liveness runs once, after the crawl, over the global de-duped set.
    result.findings.extend(await link_checker.check(link_sources))

    result.pages_scanned = len(visited)
    result.duration_seconds = round(time.monotonic() - start, 3)
    return result


async def _scan_page(
    page,
    collector: ConsoleCollector,
    url: str,
    depth: int,
    limits: RunLimits,
    queue: deque[tuple[str, int]],
    visited: set[str],
    link_sources: dict[str, str],
) -> list[Finding]:
    """Scan one page. Catches everything — a bad page is a finding, not a crash."""
    findings: list[Finding] = []
    collector.reset()
    try:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=limits.nav_timeout_ms
        )
    except PlaywrightError as exc:
        msg = str(exc).splitlines()[0] if str(exc) else "navigation failed"
        return [
            Finding.create(
                check="exploratory",
                type="page_error",
                severity=Severity.CRITICAL,
                title="Page failed to load",
                detail=f"{url} could not be loaded: {msg}",
                page_url=url,
                key="nav_error",
            )
        ]

    # Give images/sub-resources a brief chance to settle (bounded, best-effort).
    try:
        await page.wait_for_load_state("load", timeout=5_000)
    except PlaywrightError:
        pass

    findings.extend(exploratory.check_page_status(response, url))
    findings.extend(exploratory.check_console(collector, url))
    findings.extend(await exploratory.check_broken_images(page, url))
    findings.extend(await accessibility.run_axe(page, url))

    links = await exploratory.extract_links(page)
    for link in links:
        norm = normalize_url(link)
        link_sources.setdefault(norm, url)
        if depth < limits.max_depth and _same_domain(url, norm) and norm not in visited:
            queue.append((norm, depth + 1))

    return findings
