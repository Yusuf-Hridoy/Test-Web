"""Findings aggregation: group duplicates, key stably, partition signal from noise.

The crawler emits one raw finding per occurrence. This layer collapses identical
occurrences into one *distinct* finding (with an occurrence count and the affected
pages), assigns a stable grouping signature as the ``id`` (so the run-over-run diff
reconciles), and flags third-party tracker noise as informational.

Engine-free: operates purely on ``Finding`` objects.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from .findings import Finding, Severity
from .urls import normalize_message, registrable_domain, strip_volatile

# Analytics / tracker domains whose console noise is informational, not the site's bug.
_TRACKERS = (
    "clarity.ms", "googletagmanager", "google-analytics", "googlesyndication",
    "doubleclick", "mixpanel", "facebook.net", "connect.facebook", "hotjar",
    "segment.io", "segment.com", "fullstory", "intercom", "heapanalytics",
    "gtag", "analytics.google", "bat.bing", "snap.licdn", "cdn.amplitude",
)


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return registrable_domain(urlparse(url).hostname or "")


def _is_tracker(url: str | None) -> bool:
    host = (urlparse(url).hostname or "").lower() if url else ""
    return any(t in host for t in _TRACKERS)


def grouping_signature(f: Finding) -> str:
    """Stable, page-independent (where sensible) identity for a distinct problem."""
    m = f.meta or {}
    if f.check == "accessibility":
        return f"a11y:{m.get('rule_id', '?')}:{m.get('impact', '?')}"
    if f.type in ("console_error", "console_error_thirdparty"):
        return f"console:{normalize_message(m.get('message', f.detail))}"
    if f.type in ("broken_link", "link_blocked", "link_unverified"):
        return f"{f.type}:{strip_volatile(m.get('url', f.detail))}"
    if f.type == "broken_image":
        return f"image:{strip_volatile(m.get('src', f.detail))}"
    if f.type in ("page_error", "non_html"):
        return f"{f.type}:{strip_volatile(f.page_url)}"
    if f.type.startswith(("seo_", "performance")):
        return f.type  # group across pages by rule
    return f"{f.type}:{strip_volatile(f.page_url)}"


def _is_third_party(items: list[Finding]) -> bool:
    for i in items:
        if i.third_party:
            return True
        m = i.meta or {}
        if _is_tracker(m.get("source") or m.get("url") or m.get("src")):
            return True
    return False


def group(findings: list[Finding]) -> list[Finding]:
    """Collapse occurrences into distinct findings (sorted critical-first)."""
    buckets: dict[str, list[Finding]] = {}
    for f in findings:
        buckets.setdefault(grouping_signature(f), []).append(f)

    grouped: list[Finding] = []
    for sig, items in buckets.items():
        rep = min(items, key=lambda x: x.severity.rank)  # most severe representative
        pages = sorted({strip_volatile(i.page_url) for i in items if i.page_url})
        occ = len(items)
        tp = _is_third_party(items)
        detail = rep.detail
        if occ > 1 or len(pages) > 1:
            detail = f"{detail}  ·  {occ} occurrence(s) across {len(pages)} page(s)"
        # The signature is stable but unbounded (URLs/messages); hash it to a
        # fixed-length, stable id that fits finding_key and still reconciles diffs.
        fid = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]
        grouped.append(Finding(
            id=fid, check=rep.check, type=rep.type, severity=rep.severity,
            title=rep.title, detail=detail, page_url=pages[0] if pages else rep.page_url,
            evidence=rep.evidence, occurrences=occ, pages=pages,
            meta=rep.meta, third_party=tp,
        ))
    grouped.sort(key=lambda f: (f.severity.rank, f.check, f.type))
    return grouped


def is_informational(f) -> bool:
    """Informational = third-party tracker noise or info-severity (blocked/unverified
    links, non-HTML). These don't drive the headline count or the verdict.

    Tolerates both engine ``Finding`` objects and DB view DTOs (string severity,
    no ``third_party`` attribute)."""
    sev = f.severity.value if hasattr(f.severity, "value") else f.severity
    return bool(getattr(f, "third_party", False)) or sev == "info"


def partition(grouped: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Split into (actionable, informational)."""
    actionable = [f for f in grouped if not is_informational(f)]
    informational = [f for f in grouped if is_informational(f)]
    return actionable, informational


def verdict(actionable: list[Finding]) -> tuple[str, str]:
    """Health verdict from ACTIONABLE findings only (noise volume never decides)."""
    sev = {f.severity for f in actionable}
    if Severity.CRITICAL in sev:
        return "Critical issues", "fail"
    if Severity.WARNING in sev:
        return "Needs attention", "review"
    if Severity.MINOR in sev:
        return "Minor issues", "info"
    return "Healthy", "pass"
