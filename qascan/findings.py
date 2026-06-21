"""The shared output type emitted by every check.

A ``Finding`` is the only structured object that crosses the check -> report
boundary. Its ``id`` is a stable hash so Phase 3+ can diff runs ("new since
yesterday"); the hash is computed from deterministic inputs only.
"""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field

# Ordering for "critical first" report sorting.
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "minor": 2, "info": 3}


class Severity(str, Enum):
    CRITICAL = "critical"  # page won't load, 500s, form submit errors
    WARNING = "warning"  # broken link/image, console errors
    MINOR = "minor"  # accessibility nits, missing alt text
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER[self.value]


class Finding(BaseModel):
    id: str  # stable hash of (type + page_url + key)
    check: str  # "exploratory" | "accessibility"
    type: str  # "broken_link" | "console_error" | "wcag_violation" ...
    severity: Severity
    title: str  # short, human ("3 broken links")
    detail: str  # specifics (the URL, the rule, the element)
    page_url: str  # where it was found
    evidence: str | None = None  # optional screenshot path
    # Set by the aggregation layer when occurrences collapse into one finding.
    occurrences: int = 1
    pages: list[str] = Field(default_factory=list)  # deduped affected pages
    # Structured payload (rule_id/impact/targets/url/...) for rich, non-pipe rendering.
    meta: dict = Field(default_factory=dict)
    # True for third-party tracker/analytics noise (informational, off the verdict).
    third_party: bool = False

    @classmethod
    def create(
        cls,
        *,
        check: str,
        type: str,
        severity: Severity,
        title: str,
        detail: str,
        page_url: str,
        key: str | None = None,
        evidence: str | None = None,
        meta: dict | None = None,
        third_party: bool = False,
    ) -> Finding:
        """Build a finding with a deterministic ``id``.

        ``key`` is the most stable identifier for the issue (a link URL, image
        src, or rule id + target). It is hashed instead of ``detail`` so that
        cosmetic detail changes do not churn the id. Falls back to ``detail``.
        """
        basis = f"{type}|{page_url}|{key if key is not None else detail}"
        finding_id = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        return cls(
            id=finding_id,
            check=check,
            type=type,
            severity=severity,
            title=title,
            detail=detail,
            page_url=page_url,
            evidence=evidence,
            meta=meta or {},
            third_party=third_party,
        )
