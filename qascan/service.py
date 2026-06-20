"""Service layer — the single entry point the CLI *and* the UI both call.

No business logic lives in cli.py or the dashboard; it lives here. Each function
takes plain inputs (url, checks, limits, suite) and returns a structured result,
wrapping the existing engine (crawl, run_suite, generator) plus persistence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .config import RunLimits, Settings
from .crawler import CrawlResult, crawl
from .report import write_report


@dataclass
class ScanOutcome:
    crawl: CrawlResult
    out_dir: Path
    run_id: int | None = None
    diff: dict | None = None
    persist_error: str | None = None


@dataclass
class FunctionalOutcome:
    result: object  # functional.executor.RunResult
    out_dir: Path
    run_id: int | None = None
    persist_error: str | None = None


def default_limits() -> RunLimits:
    return Settings.from_env().to_limits()


def run_scan(
    url: str, *, checks: set[str] | None = None, limits: RunLimits | None = None,
    out_root: str | Path = "outputs", persist: bool = True,
    on_progress=None, cancel=None,
) -> ScanOutcome:
    """Run an oracle-free scan: crawl -> report -> (optional) persist + diff."""
    limits = limits or default_limits()
    result = asyncio.run(crawl(url, limits, checks=checks, on_progress=on_progress, cancel=cancel))
    out_dir = write_report(url, result, out_root=out_root)

    outcome = ScanOutcome(crawl=result, out_dir=out_dir)
    if persist:
        _persist_scan(url, result, outcome)
    return outcome


def _persist_scan(url: str, result: CrawlResult, outcome: ScanOutcome) -> None:
    from .db import repository
    from .db.session import DatabaseNotConfigured, session_scope

    try:
        with session_scope() as session:
            run = repository.persist_scan(session, url, result)
            session.flush()
            outcome.diff = repository.diff_findings(session, run)
            outcome.run_id = run.id
    except DatabaseNotConfigured:
        outcome.persist_error = "no_database"
    except Exception as exc:  # noqa: BLE001 — persistence never fails a scan
        outcome.persist_error = f"{type(exc).__name__}: {exc}"


def run_functional(
    suite, *, out_root: str | Path = "outputs", persist: bool = True,
    continue_on_fail: bool = False, threshold: float = 0.7,
) -> FunctionalOutcome:
    """Run a functional suite end to end, then (optionally) persist it."""
    from .functional.executor import run_suite

    result, out_dir = asyncio.run(run_suite(
        suite, out_root=out_root, continue_on_fail=continue_on_fail, threshold=threshold))
    outcome = FunctionalOutcome(result=result, out_dir=out_dir)
    if persist and result.status != "session_expired":
        _persist_functional(suite, result, outcome)
    return outcome


def _persist_functional(suite, result, outcome: FunctionalOutcome) -> None:
    from .db import repository
    from .db.session import DatabaseNotConfigured, session_scope

    try:
        with session_scope() as session:
            run = repository.persist_functional(session, suite, result)
            session.flush()
            outcome.run_id = run.id
    except DatabaseNotConfigured:
        outcome.persist_error = "no_database"
    except Exception as exc:  # noqa: BLE001
        outcome.persist_error = f"{type(exc).__name__}: {exc}"


def generate_suite(url: str, instruction: str, out_path: str | Path):
    """Draft an editable Suite from a plain instruction (compile-once)."""
    from .functional.generator import generate

    return asyncio.run(generate(url, instruction, out_path))


def capture_auth(url: str, out_path: str | Path):
    """Open a headed browser for manual login and save the storage state."""
    from .functional.auth import capture_storage_state

    return asyncio.run(capture_storage_state(url, out_path))


# Convenience for severity counts shared by CLI summary + UI cards.
def severity_counts(findings) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.severity.value if hasattr(f.severity, "value") else f.severity
        counts[sev] = counts.get(sev, 0) + 1
    return counts
