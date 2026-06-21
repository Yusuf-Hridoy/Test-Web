"""Service layer — the single entry point the CLI *and* the UI both call.

No business logic lives in cli.py or the dashboard; it lives here. Each function
takes plain inputs (url, checks, limits, suite) and returns a structured result,
wrapping the existing engine (crawl, run_suite, generator) plus persistence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .config import RunLimits, Settings
from .crawler import CrawlResult, crawl
from .report import write_report


class RunNotFound(Exception):
    """No run exists with the requested id."""


# --------------------------------------------------------------------------- #
# Read DTOs — fully-materialized plain data so the UI never holds an ORM object
# bound to a closed session (fixes DetachedInstanceError).
# --------------------------------------------------------------------------- #
class FindingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    check: str
    type: str
    severity: str
    title: str
    detail: str
    page_url: str
    evidence_path: str | None = None
    finding_key: str


class StepResultView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    case_id: int | None = None
    step_index: int
    status: str
    message: str = ""
    evidence_path: str | None = None


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    suite_id: int
    status: str
    pages_scanned: int | None = None
    stopped_reason: str | None = None
    llm_calls: int = 0
    llm_cost_estimate: float | None = None
    duration: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunDetail(BaseModel):
    run: RunView
    findings: list[FindingView]
    step_results: list[StepResultView]
    diff: dict


class SuiteView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    kind: str


class TargetView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    base_url: str
    label: str | None = None
    auth_kind: str | None = None


class HealView(BaseModel):
    id: int
    suite_id: int
    suite_name: str
    step_key: str
    selector: str
    reviewed: bool


class ScheduleView(BaseModel):
    id: int
    suite_id: int
    suite_name: str
    cron_expr: str
    enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None


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


# --------------------------------------------------------------------------- #
# Read/mutate functions for the UI — each opens AND closes its own session and
# returns plain data. The UI never imports ORM models or opens a session.
# --------------------------------------------------------------------------- #
def list_suites() -> list[SuiteView]:
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.scalars(select(models.Suite).order_by(models.Suite.name)).all()
        return [SuiteView.model_validate(r) for r in rows]


def list_runs(suite_id: int) -> list[RunView]:
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.scalars(
            select(models.Run).where(models.Run.suite_id == suite_id)
            .order_by(models.Run.id.desc())
        ).all()
        return [RunView.model_validate(r) for r in rows]


def get_run_detail(run_id: int) -> RunDetail:
    """Fully-materialized run detail. Children are eager-loaded and mapped to
    Pydantic WHILE the session is open, so the caller can use it after it closes."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .db import models, repository
    from .db.session import session_scope

    with session_scope() as s:
        run = s.scalars(
            select(models.Run).where(models.Run.id == run_id).options(
                selectinload(models.Run.findings),
                selectinload(models.Run.step_results),
            )
        ).first()
        if run is None:
            raise RunNotFound(f"run #{run_id} not found")
        diff = repository.diff_findings(s, run)
        return RunDetail(
            run=RunView.model_validate(run),
            findings=[FindingView.model_validate(f) for f in run.findings],
            step_results=[StepResultView.model_validate(x) for x in run.step_results],
            diff=diff,
        )


def list_targets() -> list[TargetView]:
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.scalars(select(models.Target).order_by(models.Target.id)).all()
        return [TargetView.model_validate(t) for t in rows]


def add_target(base_url: str, label: str | None = None) -> None:
    from sqlalchemy import select

    from .db import models, repository
    from .db.session import session_scope

    with session_scope() as s:
        repository.get_or_create_target(s, base_url)
        if label:
            tgt = s.scalar(select(models.Target).where(models.Target.base_url == base_url))
            if tgt:
                tgt.label = label


def list_pending_heals() -> list[HealView]:
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.scalars(
            select(models.SelectorCache).where(models.SelectorCache.reviewed.is_(False))
            .order_by(models.SelectorCache.id)
        ).all()
        out = []
        for r in rows:
            suite = s.get(models.Suite, r.suite_id)
            out.append(HealView(id=r.id, suite_id=r.suite_id,
                                suite_name=suite.name if suite else str(r.suite_id),
                                step_key=r.step_key, selector=r.selector, reviewed=r.reviewed))
        return out


def approve_heal(heal_id: int) -> None:
    from .db import models, repository
    from .db.session import session_scope

    with session_scope() as s:
        row = s.get(models.SelectorCache, heal_id)
        if row:
            repository.approve_heal(s, row)


def reject_heal(heal_id: int) -> None:
    from .db import models, repository
    from .db.session import session_scope

    with session_scope() as s:
        row = s.get(models.SelectorCache, heal_id)
        if row:
            suite = s.get(models.Suite, row.suite_id)
            repository.reject_heal(s, row, suite.name if suite else "")


def list_schedules() -> list[ScheduleView]:
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        out = []
        for sched in s.scalars(select(models.Schedule).order_by(models.Schedule.id)).all():
            suite = s.get(models.Suite, sched.suite_id)
            out.append(ScheduleView(
                id=sched.id, suite_id=sched.suite_id,
                suite_name=suite.name if suite else str(sched.suite_id),
                cron_expr=sched.cron_expr, enabled=sched.enabled,
                last_run_at=sched.last_run_at, next_run_at=sched.next_run_at))
        return out


def add_schedule(suite_id: int, cron_expr: str) -> None:
    from .db import repository
    from .db.session import session_scope

    with session_scope() as s:
        repository.add_schedule(s, suite_id, cron_expr)


def toggle_schedule(schedule_id: int) -> None:
    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        sched = s.get(models.Schedule, schedule_id)
        if sched:
            sched.enabled = not sched.enabled


def delete_schedule(schedule_id: int) -> None:
    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        sched = s.get(models.Schedule, schedule_id)
        if sched:
            s.delete(sched)


# Convenience for severity counts shared by CLI summary + UI cards.
def severity_counts(findings) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.severity.value if hasattr(f.severity, "value") else f.severity
        counts[sev] = counts.get(sev, 0) + 1
    return counts
