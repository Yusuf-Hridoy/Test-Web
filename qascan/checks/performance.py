"""Performance via Lighthouse (opt-in, heavy).

Runs the Lighthouse CLI against a URL and maps Core-Web-Vitals-style metrics to
findings with universal (oracle-free) thresholds. Requires Node + Lighthouse in
the runtime (``npm i -g lighthouse``); if unavailable, emits one INFO finding
rather than crashing. Heavy, so it is off by default and runs once on the seed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from ..findings import Finding, Severity

CHECK = "performance"
log = logging.getLogger("qascan.performance")

_LH_TIMEOUT = 120.0  # seconds — Lighthouse is slow; bound it hard

# (audit_id, label, unit, minor_threshold, warning_threshold)
_METRICS = [
    ("largest-contentful-paint", "LCP", "ms", 2500, 4000),
    ("cumulative-layout-shift", "CLS", "", 0.1, 0.25),
    ("total-blocking-time", "TBT", "ms", 200, 600),
]


def _cmd(url: str) -> list[str] | None:
    flags = [
        url, "--output=json", "--output-path=stdout", "--quiet",
        "--only-categories=performance",
        "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
    ]
    exe = shutil.which("lighthouse")
    if exe:
        return [exe, *flags]
    if shutil.which("npx"):
        return ["npx", "--yes", "lighthouse", *flags]
    return None


def _env() -> dict:
    """Point Lighthouse at Playwright's Chromium if no system Chrome is set."""
    env = dict(os.environ)
    if "CHROME_PATH" not in env:
        try:
            # Resolve the installed chromium executable lazily.
            import glob

            from playwright.sync_api import sync_playwright  # noqa: F401  (path lookup)

            matches = glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app"
                "/Contents/MacOS/Chromium"))
            if matches:
                env["CHROME_PATH"] = sorted(matches)[-1]
        except Exception:  # noqa: BLE001
            pass
    return env


def _unavailable(url: str, reason: str) -> list[Finding]:
    log.warning("performance check skipped: %s", reason)
    return [Finding.create(
        check=CHECK, type="performance_unavailable", severity=Severity.INFO,
        title="Performance check unavailable",
        detail=f"Lighthouse did not run ({reason}). Install Node + `lighthouse`.",
        page_url=url, key="unavailable")]


async def run(url: str) -> list[Finding]:
    """Run Lighthouse on ``url`` and map metrics to findings (oracle-free thresholds)."""
    cmd = _cmd(url)
    if cmd is None:
        return _unavailable(url, "Node/Lighthouse not found on PATH")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_LH_TIMEOUT)
    except TimeoutError:
        return _unavailable(url, "Lighthouse timed out")
    except OSError as exc:
        return _unavailable(url, f"could not launch Lighthouse: {exc}")

    if proc.returncode != 0 or not stdout:
        return _unavailable(url, (stderr.decode()[:160] or "non-zero exit").strip())
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError:
        return _unavailable(url, "could not parse Lighthouse JSON")

    return _map(url, report)


def _map(url: str, report: dict) -> list[Finding]:
    findings: list[Finding] = []
    audits = report.get("audits", {})

    score = (report.get("categories", {}).get("performance", {}) or {}).get("score")
    if score is not None:
        pct = round(score * 100)
        if score < 0.5:
            findings.append(_f(url, "performance_score", Severity.WARNING,
                               f"Low performance score ({pct})",
                               f"Lighthouse performance score {pct}/100 (poor)."))
        elif score < 0.9:
            findings.append(_f(url, "performance_score", Severity.MINOR,
                               f"Performance score {pct}",
                               f"Lighthouse performance score {pct}/100 (needs improvement)."))

    for audit_id, label, unit, minor_t, warn_t in _METRICS:
        audit = audits.get(audit_id) or {}
        value = audit.get("numericValue")
        if value is None:
            continue
        shown = f"{round(value)}{unit}" if unit else f"{value:.3f}"
        if value > warn_t:
            sev = Severity.WARNING
        elif value > minor_t:
            sev = Severity.MINOR
        else:
            continue  # within "good" — no finding (keeps fast sites clean)
        findings.append(_f(url, f"performance_{audit_id}", sev,
                           f"{label} {shown}",
                           f"{label} is {shown} (threshold {minor_t}{unit}/{warn_t}{unit})."))
    return findings


def _f(url, type_, sev, title, detail) -> Finding:
    return Finding.create(check=CHECK, type=type_, severity=sev, title=title,
                          detail=detail, page_url=url, key=type_)
