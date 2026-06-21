"""Accessibility check via vendored axe-core.

axe-core is bundled in ``vendor/axe.min.js`` and injected into the page — never
fetched from a CDN at runtime. Each axe ``violation`` becomes one ``Finding``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..findings import Finding, Severity

CHECK = "accessibility"

# Repo-root/vendor/axe.min.js  (checks -> qascan -> repo root).
# Vendored axe-core version: 4.10.2 (do not fetch from CDN at runtime).
AXE_PATH = Path(__file__).resolve().parents[2] / "vendor" / "axe.min.js"

# axe impact drives display severity directly, so the badge never contradicts the
# stated impact and the summary's "Critical" count is real (Fix 3).
_IMPACT_TO_SEVERITY = {
    "critical": Severity.CRITICAL,
    "serious": Severity.WARNING,
    "moderate": Severity.MINOR,
    "minor": Severity.MINOR,
}

_MAX_NODES_PER_RULE = 5

_AXE_RUN_JS = "async () => await axe.run()"


async def run_axe(page: Page, url: str, axe_path: Path | None = None) -> list[Finding]:
    """Inject axe-core, run it, and map violations to findings.

    Returns ``[]`` on any injection/run failure rather than aborting the scan.
    """
    path = axe_path or AXE_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Vendored axe-core not found at {path}. "
            "Bundle it into vendor/axe.min.js (see docs/phase1.md)."
        )
    try:
        await asyncio.wait_for(page.add_script_tag(path=str(path)), timeout=10.0)
        results = await asyncio.wait_for(page.evaluate(_AXE_RUN_JS), timeout=30.0)
    except (PlaywrightError, TimeoutError):
        # axe failed or took too long — a missed a11y check, never a hung run.
        return []

    findings: list[Finding] = []
    for violation in results.get("violations", []):
        rule_id = violation.get("id", "unknown")
        impact = violation.get("impact") or "minor"
        severity = _IMPACT_TO_SEVERITY.get(impact, Severity.MINOR)
        help_text = violation.get("help", rule_id)
        help_url = violation.get("helpUrl", "")
        nodes = violation.get("nodes", [])
        all_targets: list[str] = []
        for node in nodes:
            target = node.get("target", [])
            all_targets.append(target[0] if target else "<unknown>")
        shown_targets = all_targets[:_MAX_NODES_PER_RULE]

        # Readable detail (still contains the rule id for searchability); the rich,
        # structured view is rendered from `meta`, not this string.
        detail = f"{help_text} (rule {rule_id}, axe impact {impact})"

        findings.append(
            Finding.create(
                check=CHECK,
                type="wcag_violation",
                severity=severity,
                title=help_text,
                detail=detail,
                page_url=url,
                key=f"{rule_id}:{all_targets[0] if all_targets else ''}",
                meta={
                    "rule_id": rule_id,
                    "impact": impact,
                    "help_url": help_url,
                    "targets": shown_targets,
                    "total_elements": len(nodes),
                },
            )
        )
    return findings
