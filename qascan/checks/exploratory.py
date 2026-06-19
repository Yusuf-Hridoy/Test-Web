"""Exploratory checks: page load status, console/page errors, broken images,
broken links.

Each function returns ``list[Finding]`` and never raises for an unhealthy page
— a broken page is a result, not an exception. Link liveness is checked once
per unique URL across the whole crawl (the cache lives in ``LinkChecker``).
"""

from __future__ import annotations

import asyncio

import httpx
from playwright.async_api import ConsoleMessage, Page, Response
from playwright.async_api import Error as PlaywrightError

from ..config import RunLimits
from ..findings import Finding, Severity

CHECK = "exploratory"

# Hrefs we never treat as navigable / checkable links.
_SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "blob:", "about:")


class ConsoleCollector:
    """Captures ``console.error`` output and uncaught page exceptions.

    Attach once to a reused page; call :meth:`reset` before each navigation so
    messages are scoped to the page currently being scanned.
    """

    def __init__(self) -> None:
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []

    def attach(self, page: Page) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)

    def _on_console(self, msg: ConsoleMessage) -> None:
        if msg.type == "error":
            self.console_errors.append(msg.text)

    def _on_pageerror(self, exc: PlaywrightError) -> None:
        self.page_errors.append(str(exc).splitlines()[0] if str(exc) else "uncaught error")

    def reset(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()


def check_page_status(response: Response | None, url: str) -> list[Finding]:
    """Non-2xx/3xx response -> critical ``page_error``."""
    if response is None:
        # Navigation produced no response object (e.g. about:blank); not fatal.
        return []
    status = response.status
    if status >= 400:
        return [
            Finding.create(
                check=CHECK,
                type="page_error",
                severity=Severity.CRITICAL,
                title=f"Page returned HTTP {status}",
                detail=f"{url} responded with status {status}.",
                page_url=url,
                key=str(status),
            )
        ]
    return []


def check_console(collector: ConsoleCollector, url: str) -> list[Finding]:
    """Drain captured console errors and uncaught exceptions into findings."""
    findings: list[Finding] = []
    for text in collector.console_errors:
        findings.append(
            Finding.create(
                check=CHECK,
                type="console_error",
                severity=Severity.WARNING,
                title="Console error",
                detail=text,
                page_url=url,
                key=text,
            )
        )
    for text in collector.page_errors:
        findings.append(
            Finding.create(
                check=CHECK,
                type="console_error",
                severity=Severity.WARNING,
                title="Uncaught JavaScript exception",
                detail=text,
                page_url=url,
                key=text,
            )
        )
    return findings


_BROKEN_IMG_JS = """
() => Array.from(document.images)
    .filter(img => img.complete && img.naturalWidth === 0)
    .map(img => img.currentSrc || img.src || img.getAttribute('src') || '')
    .filter(src => src.length > 0)
"""


async def check_broken_images(page: Page, url: str) -> list[Finding]:
    """In-page check: images that finished loading with zero natural width."""
    try:
        srcs: list[str] = await page.evaluate(_BROKEN_IMG_JS)
    except PlaywrightError:
        return []
    findings: list[Finding] = []
    for src in dict.fromkeys(srcs):  # dedupe, preserve order
        findings.append(
            Finding.create(
                check=CHECK,
                type="broken_image",
                severity=Severity.WARNING,
                title="Broken image",
                detail=f"Image failed to load: {src}",
                page_url=url,
                key=src,
            )
        )
    return findings


_EXTRACT_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]'))
    .map(a => a.href)
    .filter(h => h && h.length > 0)
"""


async def extract_links(page: Page) -> list[str]:
    """Return absolute hrefs on the page, fragments stripped, junk schemes dropped."""
    try:
        hrefs: list[str] = await page.evaluate(_EXTRACT_LINKS_JS)
    except PlaywrightError:
        return []
    cleaned: list[str] = []
    for href in hrefs:
        href = href.split("#", 1)[0]
        if not href or href.lower().startswith(_SKIP_PREFIXES):
            continue
        cleaned.append(href)
    return list(dict.fromkeys(cleaned))


class LinkChecker:
    """Liveness checker with a process-wide cache so each URL is hit once."""

    def __init__(self, limits: RunLimits) -> None:
        self._limits = limits
        self._sem = asyncio.Semaphore(limits.link_concurrency)
        # url -> status code (or None for a connection/timeout error).
        self._cache: dict[str, int | None] = {}

    async def _probe(self, client: httpx.AsyncClient, url: str) -> int | None:
        if url in self._cache:
            return self._cache[url]
        async with self._sem:
            status: int | None
            try:
                resp = await client.head(url)
                if resp.status_code == 405:  # method not allowed -> retry with GET
                    resp = await client.get(url)
                status = resp.status_code
            except httpx.HTTPError:
                status = None
            self._cache[url] = status
            return status

    async def check(self, link_sources: dict[str, str]) -> list[Finding]:
        """Check a ``{url: page_url_where_first_seen}`` map; emit broken_link findings.

        A URL is broken if it returns 4xx/5xx or fails to connect/times out.
        """
        findings: list[Finding] = []
        headers = {"User-Agent": "qascan/0.1 (+https://github.com/qascan)"}
        timeout = httpx.Timeout(self._limits.link_timeout_seconds)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=headers
        ) as client:
            urls = list(link_sources)
            statuses = await asyncio.gather(*(self._probe(client, u) for u in urls))
            for url, status in zip(urls, statuses, strict=True):
                if status is None:
                    detail = f"Link could not be reached (connection error or timeout): {url}"
                elif status >= 400:
                    detail = f"Link returned HTTP {status}: {url}"
                else:
                    continue
                findings.append(
                    Finding.create(
                        check=CHECK,
                        type="broken_link",
                        severity=Severity.WARNING,
                        title="Broken link",
                        detail=detail,
                        page_url=link_sources[url],
                        key=url,
                    )
                )
        return findings
