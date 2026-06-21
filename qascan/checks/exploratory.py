"""Exploratory checks: page load status, console/page errors, broken images,
broken links.

Each function returns ``list[Finding]`` and never raises for an unhealthy page.
Accuracy guards (Phase 5): console errors are split first-party vs third-party;
link liveness distinguishes *broken* (404/4xx/5xx) from *blocked/unverified*
(403/429/timeout) so rate-limiting and bot-blocking don't read as dead links.
"""

from __future__ import annotations

import asyncio

import httpx
from playwright.async_api import ConsoleMessage, Page, Response
from playwright.async_api import Error as PlaywrightError

from ..config import RunLimits
from ..findings import Finding, Severity
from ..urls import is_third_party

CHECK = "exploratory"

_SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "blob:", "about:")
_EVAL_TIMEOUT = 10.0  # seconds — nothing in-page may hang the run


class ConsoleCollector:
    """Captures ``console.error`` (with source URL) and uncaught page exceptions."""

    def __init__(self) -> None:
        self.console_errors: list[tuple[str, str]] = []  # (text, source_url)
        self.page_errors: list[str] = []

    def attach(self, page: Page) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)

    def _on_console(self, msg: ConsoleMessage) -> None:
        if msg.type == "error":
            try:
                src = (msg.location or {}).get("url", "")
            except Exception:  # noqa: BLE001
                src = ""
            self.console_errors.append((msg.text, src))

    def _on_pageerror(self, exc) -> None:
        self.page_errors.append(str(exc).splitlines()[0] if str(exc) else "uncaught error")

    def reset(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()


def check_page_status(response: Response | None, url: str) -> list[Finding]:
    """Non-2xx/3xx response -> critical ``page_error``."""
    if response is None:
        return []
    if response.status >= 400:
        return [Finding.create(
            check=CHECK, type="page_error", severity=Severity.CRITICAL,
            title=f"Page returned HTTP {response.status}",
            detail=f"{url} responded with status {response.status}.",
            page_url=url, key=str(response.status),
        )]
    return []


def check_console(collector: ConsoleCollector, url: str) -> list[Finding]:
    """Drain console errors (split first/third-party) and uncaught exceptions."""
    findings: list[Finding] = []
    for text, src in collector.console_errors:
        if is_third_party(url, src):
            findings.append(Finding.create(
                check=CHECK, type="console_error_thirdparty", severity=Severity.MINOR,
                title="Console error (third-party script)",
                detail=f"{text}  [source: {src}]", page_url=url, key=f"3p|{text}",
                meta={"message": text, "source": src}, third_party=True,
            ))
        else:
            findings.append(Finding.create(
                check=CHECK, type="console_error", severity=Severity.WARNING,
                title="Console error", detail=text, page_url=url, key=text,
                meta={"message": text, "source": src},
            ))
    for text in collector.page_errors:
        findings.append(Finding.create(
            check=CHECK, type="console_error", severity=Severity.WARNING,
            title="Uncaught JavaScript exception", detail=text, page_url=url, key=text,
            meta={"message": text},
        ))
    return findings


_BROKEN_IMG_JS = """
() => Array.from(document.images)
    .filter(img => img.complete && img.naturalWidth === 0)
    .map(img => img.currentSrc || img.src || img.getAttribute('src') || '')
    .filter(src => src.length > 0)
"""


async def _bounded_eval(page: Page, script: str, default):
    """page.evaluate with a hard timeout — never let in-page JS hang a run."""
    try:
        return await asyncio.wait_for(page.evaluate(script), timeout=_EVAL_TIMEOUT)
    except (PlaywrightError, TimeoutError):
        return default


async def check_broken_images(page: Page, url: str) -> list[Finding]:
    """In-page check: images that finished loading with zero natural width."""
    srcs: list[str] = await _bounded_eval(page, _BROKEN_IMG_JS, [])
    findings: list[Finding] = []
    for src in dict.fromkeys(srcs):
        findings.append(Finding.create(
            check=CHECK, type="broken_image", severity=Severity.WARNING,
            title="Broken image", detail=f"Image failed to load: {src}",
            page_url=url, key=src, meta={"src": src},
        ))
    return findings


_EXTRACT_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]'))
    .map(a => a.href).filter(h => h && h.length > 0)
"""


async def extract_links(page: Page) -> list[str]:
    """Return absolute hrefs on the page, fragments stripped, junk schemes dropped."""
    hrefs: list[str] = await _bounded_eval(page, _EXTRACT_LINKS_JS, [])
    cleaned: list[str] = []
    for href in hrefs:
        href = href.split("#", 1)[0]
        if not href or href.lower().startswith(_SKIP_PREFIXES):
            continue
        cleaned.append(href)
    return list(dict.fromkeys(cleaned))


# HTTP statuses that mean "we couldn't verify" (blocked / rate-limited), NOT broken.
_BLOCKED_STATUSES = {401, 403, 429}


class LinkChecker:
    """Liveness checker with a process-wide cache so each URL is hit once."""

    def __init__(self, limits: RunLimits) -> None:
        self._limits = limits
        self._sem = asyncio.Semaphore(limits.link_concurrency)
        self._cache: dict[str, int | None] = {}

    async def _probe(self, client: httpx.AsyncClient, url: str) -> int | None:
        if url in self._cache:
            return self._cache[url]
        async with self._sem:
            try:
                resp = await client.head(url)
                if resp.status_code in (405, 501):  # method not allowed -> GET
                    resp = await client.get(url)
                status: int | None = resp.status_code
            except httpx.HTTPError:
                status = None
            self._cache[url] = status
            return status

    async def check(self, link_sources: dict[str, str]) -> list[Finding]:
        """Classify each unique link: broken vs blocked/unverified vs ok."""
        findings: list[Finding] = []
        headers = {"User-Agent": "Mozilla/5.0 (compatible; qascan/0.1; +qascan)"}
        timeout = httpx.Timeout(self._limits.link_timeout_seconds)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=headers
        ) as client:
            urls = list(link_sources)
            statuses = await asyncio.gather(*(self._probe(client, u) for u in urls))
            for url, status in zip(urls, statuses, strict=True):
                page_url = link_sources[url]
                if status is None:
                    # Could not connect/timed out — unknown, not provably broken.
                    findings.append(Finding.create(
                        check=CHECK, type="link_unverified", severity=Severity.INFO,
                        title="Link could not be verified",
                        detail=f"No response (timeout/connection error): {url}",
                        page_url=page_url, key=url, meta={"url": url, "status": None},
                    ))
                elif status in _BLOCKED_STATUSES:
                    findings.append(Finding.create(
                        check=CHECK, type="link_blocked", severity=Severity.INFO,
                        title="Link blocked (could not verify)",
                        detail=f"HTTP {status} (rate-limited or bot-blocked): {url}",
                        page_url=page_url, key=url, meta={"url": url, "status": status},
                    ))
                elif status >= 400:
                    findings.append(Finding.create(
                        check=CHECK, type="broken_link", severity=Severity.WARNING,
                        title="Broken link", detail=f"Link returned HTTP {status}: {url}",
                        page_url=page_url, key=url, meta={"url": url, "status": status},
                    ))
        return findings
