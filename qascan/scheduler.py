"""Scheduler + scheduled-run execution (Phase 4).

APScheduler with a Postgres-backed job store (jobs survive restart). Each scheduled
run reuses the exact Phase 2/3 path — no special-casing. Overlap is prevented with a
Postgres advisory lock so even a manual "run now" can't collide with the worker.
Failed functional cases are retried once: pass-on-retry is marked *flaky* (no alert);
fail-twice is a real failure.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Engine, text

from . import notify
from .config import Settings
from .crawler import crawl
from .db import models, repository
from .db.session import database_url, get_engine, session_scope
from .functional.executor import run_suite

_scheduler = None  # set by worker(); used by the reconcile job


# --------------------------------------------------------------------------- #
# Overlap guard — Postgres advisory lock keyed by suite id
# --------------------------------------------------------------------------- #
@contextmanager
def suite_lock(engine: Engine, suite_id: int):
    """Yield True if the per-suite advisory lock was acquired, else False.

    Cross-process: a manual run and the worker contend on the same key.
    """
    conn = engine.connect()
    got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": suite_id}).scalar()
    try:
        yield bool(got)
    finally:
        if got:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": suite_id})
            conn.commit()
        conn.close()


# --------------------------------------------------------------------------- #
# Flaky-aware functional run (retry failed cases once)
# --------------------------------------------------------------------------- #
async def run_with_retry(suite, *, threshold: float = 0.7, out_root="outputs", retry: bool = True):
    """Run a functional suite; retry failed cases once. Returns (result, out_dir,
    flaky_case_ids). A case that fails then passes is relabelled 'flaky'."""
    from .functional.schema import Suite

    result, out_dir = await run_suite(suite, continue_on_fail=True, threshold=threshold,
                                      out_root=out_root)
    flaky: set[str] = set()
    failed_ids = {c.id for c in result.cases if c.status == "fail"}
    if retry and failed_ids:
        subset = Suite(name=suite.name, target=suite.target,
                       cases=[c for c in suite.cases if c.id in failed_ids])
        retry_result, _ = await run_suite(subset, continue_on_fail=True, threshold=threshold,
                                          out_root=out_root)
        retry_status = {c.id: c.status for c in retry_result.cases}
        for case in result.cases:
            if case.status == "fail" and retry_status.get(case.id) == "pass":
                case.status = "flaky"
                flaky.add(case.id)
                for step in case.steps:
                    if step.status == "fail":
                        step.status = "flaky"
        if any(c.status == "fail" for c in result.cases):
            result.status = "fail"
        elif any(c.status == "needs_review" for c in result.cases):
            result.status = "needs_review"
        elif flaky:
            result.status = "flaky"
        else:
            result.status = "pass"
    return result, out_dir, flaky


# --------------------------------------------------------------------------- #
# Executing one suite (scan or functional) end to end
# --------------------------------------------------------------------------- #
def execute_suite(suite_id: int, *, engine: Engine | None = None) -> dict:
    """Run + persist + notify one suite. Skips if a run is already in progress."""
    engine = engine or get_engine()
    with suite_lock(engine, suite_id) as acquired:
        if not acquired:
            return {"suite_id": suite_id, "skipped": "overlap"}
        return _execute(suite_id)


def _execute(suite_id: int) -> dict:
    with session_scope() as session:
        suite = session.get(models.Suite, suite_id)
        if suite is None:
            return {"suite_id": suite_id, "error": "suite not found"}
        if suite.kind == "scan":
            return _execute_scan(session, suite)
        return _execute_functional(session, suite)


def _execute_scan(session, suite: models.Suite) -> dict:
    target = session.get(models.Target, suite.target_id)
    limits = Settings.from_env().to_limits()
    result = asyncio.run(crawl(target.base_url, limits))
    run = repository.persist_scan(session, target.base_url, result)
    session.flush()
    diff = repository.diff_findings(session, run)
    ctx = notify.build_context(suite.name, run, diff, kind="scan", failed_cases=[])
    sent = notify.notify_for_run(session, suite.id, ctx)
    repository.mark_schedule_ran(session, suite.id, datetime.now(UTC))
    return {"suite_id": suite.id, "run_id": run.id, "status": run.status,
            "new": len(diff["new"]), "notified": sent}


def _execute_functional(session, suite: models.Suite) -> dict:
    schema_suite = repository.rebuild_functional_suite(session, suite.id)
    result, _out, flaky = asyncio.run(run_with_retry(schema_suite))
    run = repository.persist_functional(session, schema_suite, result)
    session.flush()
    diff = repository.diff_findings(session, run)
    failed = [c.name for c in result.cases if c.status == "fail"]  # flaky excluded
    ctx = notify.build_context(suite.name, run, diff, kind="functional", failed_cases=failed)
    sent = notify.notify_for_run(session, suite.id, ctx)
    repository.mark_schedule_ran(session, suite.id, datetime.now(UTC))
    return {"suite_id": suite.id, "run_id": run.id, "status": run.status,
            "flaky": sorted(flaky), "failed": failed, "notified": sent}


# Top-level reference for the persistent job store (module:function).
def run_scheduled(suite_id: int) -> None:
    execute_suite(suite_id)


# --------------------------------------------------------------------------- #
# Scheduler wiring
# --------------------------------------------------------------------------- #
def build_scheduler(*, blocking: bool = False, url: str | None = None):
    jobstores = {"default": SQLAlchemyJobStore(url=url or database_url(),
                                               tablename="apscheduler_jobs")}
    defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
    cls = BlockingScheduler if blocking else BackgroundScheduler
    return cls(jobstores=jobstores, job_defaults=defaults)


def sync_schedules(scheduler, session) -> int:
    """Add/update jobs for enabled schedules; remove jobs whose schedule is gone."""
    enabled = repository.enabled_schedules(session)
    wanted = set()
    for sched in enabled:
        job_id = f"suite:{sched.suite_id}"
        wanted.add(job_id)
        scheduler.add_job(
            run_scheduled, CronTrigger.from_crontab(sched.cron_expr),
            args=[sched.suite_id], id=job_id, replace_existing=True,
        )
    for job in scheduler.get_jobs():
        if job.id.startswith("suite:") and job.id not in wanted:
            job.remove()
    return len(wanted)


def _reconcile() -> None:
    if _scheduler is not None:
        with session_scope() as session:
            sync_schedules(_scheduler, session)


def worker() -> None:
    """Long-running worker: load schedules and trigger runs (blocks)."""
    global _scheduler
    _scheduler = build_scheduler(blocking=True)
    with session_scope() as session:
        count = sync_schedules(_scheduler, session)
    print(f"qascan worker: loaded {count} enabled schedule(s). Ctrl-C to stop.", flush=True)
    # Periodically pick up schedule changes made via the dashboard/CLI.
    _scheduler.add_job(_reconcile, "interval", seconds=30, id="reconcile",
                       replace_existing=True)
    _scheduler.start()
