"""Persistence + run diffing.

A scan or functional run is written as one ``Run`` with all its children in a
single transaction. ``diff_findings`` compares a run against the previous run on
the same suite using the stable ``finding_key``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

# Rough per-call cost for gemini-2.5-flash-lite (USD). Estimate only — refined in
# the billing phase. Keeps llm_cost_estimate populated for the dashboard.
COST_PER_LLM_CALL = 0.0002


def _scan_verdict(findings) -> str:
    sev = {f.severity.value if hasattr(f.severity, "value") else f.severity for f in findings}
    if "critical" in sev:
        return "critical"
    if "warning" in sev:
        return "warning"
    if "minor" in sev:
        return "minor"
    return "healthy"


def get_or_create_target(
    session: Session, base_url: str, auth_kind: str | None = None
) -> models.Target:
    target = session.scalar(select(models.Target).where(models.Target.base_url == base_url))
    if target is None:
        target = models.Target(base_url=base_url, label=urlparse(base_url).hostname,
                               auth_kind=auth_kind)
        session.add(target)
        session.flush()
    elif auth_kind and target.auth_kind != auth_kind:
        target.auth_kind = auth_kind
    return target


def get_or_create_suite(
    session: Session, target: models.Target, name: str, kind: str
) -> models.Suite:
    suite = session.scalar(
        select(models.Suite).where(
            models.Suite.target_id == target.id,
            models.Suite.name == name,
            models.Suite.kind == kind,
        )
    )
    if suite is None:
        suite = models.Suite(target_id=target.id, name=name, kind=kind)
        session.add(suite)
        session.flush()
    return suite


# --------------------------------------------------------------------------- #
# Phase 1 — scan persistence
# --------------------------------------------------------------------------- #
def persist_scan(session: Session, url: str, crawl_result) -> models.Run:
    """Write a scan crawl result as a Run with Finding children."""
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    target = get_or_create_target(session, base)
    suite = get_or_create_suite(session, target, name=urlparse(url).hostname or url, kind="scan")

    run = models.Run(
        suite_id=suite.id,
        finished_at=datetime.now(UTC),
        status=_scan_verdict(crawl_result.findings),
        pages_scanned=crawl_result.pages_scanned,
        stopped_reason=crawl_result.stopped_reason,
        llm_calls=0,
        llm_cost_estimate=0.0,
        duration=crawl_result.duration_seconds,
    )
    session.add(run)
    session.flush()

    for f in crawl_result.findings:
        session.add(models.Finding(
            run_id=run.id,
            check=f.check,
            type=f.type,
            severity=f.severity.value if hasattr(f.severity, "value") else f.severity,
            title=f.title,
            detail=f.detail,
            page_url=f.page_url,
            evidence_path=f.evidence,
            finding_key=f.id,
        ))
    return run


# --------------------------------------------------------------------------- #
# Phase 2 — functional run persistence
# --------------------------------------------------------------------------- #
def persist_functional(session: Session, suite_schema, run_result) -> models.Run:
    """Write a functional RunResult (+ its Suite's cases, step results, heals)."""
    auth_kind = suite_schema.target.auth.kind if suite_schema.target.auth else None
    target = get_or_create_target(session, suite_schema.target.base_url, auth_kind)
    suite = get_or_create_suite(session, target, name=suite_schema.name, kind="functional")

    # Upsert test cases; keep a name -> id map for step_results.
    case_ids: dict[str, int] = {}
    for case in suite_schema.cases:
        steps = [s.model_dump(by_alias=True, exclude_none=True) for s in case.steps]
        row = session.scalar(
            select(models.TestCase).where(
                models.TestCase.suite_id == suite.id, models.TestCase.name == case.name
            )
        )
        if row is None:
            row = models.TestCase(
                suite_id=suite.id, name=case.name, case_key=case.id,
                source=case.source, steps=steps,
            )
            session.add(row)
            session.flush()
        else:
            row.case_key = case.id
            row.source = case.source
            row.steps = steps
        case_ids[case.id] = row.id

    run = models.Run(
        suite_id=suite.id,
        finished_at=datetime.now(UTC),
        status=run_result.status,
        llm_calls=run_result.llm_calls,
        llm_cost_estimate=round(run_result.llm_calls * COST_PER_LLM_CALL, 6),
        duration=run_result.duration_seconds,
    )
    session.add(run)
    session.flush()

    for case in run_result.cases:
        cid = case_ids.get(case.id)
        for step in case.steps:
            session.add(models.StepResult(
                run_id=run.id,
                case_id=cid,
                step_index=step.index,
                status=step.status,
                message=step.message,
                evidence_path=step.evidence,
            ))

    _upsert_selector_cache(session, suite, run_result.healed_for_review)
    return run


def _upsert_selector_cache(session: Session, suite: models.Suite, healed: list[dict]) -> None:
    for h in healed:
        step_key = h.get("step")
        if not step_key:
            continue
        row = session.scalar(
            select(models.SelectorCache).where(
                models.SelectorCache.suite_id == suite.id,
                models.SelectorCache.step_key == step_key,
            )
        )
        if row is None:
            session.add(models.SelectorCache(
                suite_id=suite.id, step_key=step_key,
                selector=h.get("selector", ""), reviewed=bool(h.get("reviewed", False)),
            ))
        elif row.selector != h.get("selector", ""):
            # The element moved again -> a fresh heal; needs review afresh.
            row.selector = h.get("selector", "")
            row.reviewed = False


# --------------------------------------------------------------------------- #
# Heal review-queue actions (used by the dashboard)
# --------------------------------------------------------------------------- #
def approve_heal(session: Session, row: models.SelectorCache) -> None:
    """Trust a healed selector going forward."""
    row.reviewed = True


def reject_heal(session: Session, row: models.SelectorCache, suite_name: str,
                cache_root=None) -> None:
    """Reject a heal: drop the DB row AND invalidate the runtime file cache so the
    step re-heals on the next run."""
    from ..functional.locators import HealCache

    HealCache(suite_name, root=cache_root).remove(row.step_key)
    session.delete(row)


# --------------------------------------------------------------------------- #
# Rebuilding a runnable suite from the DB (for scheduled runs)
# --------------------------------------------------------------------------- #
def rebuild_functional_suite(session: Session, suite_id: int):
    """Reconstruct a schema.Suite from stored test cases so a scheduled run can
    reuse the exact Phase-2 run path. (Auth config beyond auth_kind is not yet
    persisted, so reconstructed suites run unauthenticated.)"""
    from ..functional.schema import Step, Suite, TargetConfig, TestCase

    suite = session.get(models.Suite, suite_id)
    if suite is None or suite.kind != "functional":
        raise ValueError(f"Suite {suite_id} is not a functional suite.")
    target = session.get(models.Target, suite.target_id)
    cases = []
    for row in suite.cases:
        steps = [Step.model_validate(s) for s in (row.steps or [])]
        cases.append(TestCase(id=row.case_key or row.name, name=row.name,
                              steps=steps, source=row.source))
    return Suite(name=suite.name, target=TargetConfig(base_url=target.base_url), cases=cases)


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #
def add_schedule(session: Session, suite_id: int, cron_expr: str,
                 enabled: bool = True) -> models.Schedule:
    sched = models.Schedule(suite_id=suite_id, cron_expr=cron_expr, enabled=enabled)
    session.add(sched)
    session.flush()
    return sched


def enabled_schedules(session: Session) -> list[models.Schedule]:
    return list(session.scalars(
        select(models.Schedule).where(models.Schedule.enabled.is_(True))
    ).all())


def mark_schedule_ran(session: Session, suite_id: int, last_run, next_run=None) -> None:
    sched = session.scalar(select(models.Schedule).where(models.Schedule.suite_id == suite_id))
    if sched:
        sched.last_run_at = last_run
        if next_run is not None:
            sched.next_run_at = next_run


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #
def diff_findings(session: Session, run: models.Run) -> dict[str, list[dict]]:
    """New / resolved / persisting findings vs the previous run on the same suite."""
    prev = session.scalar(
        select(models.Run)
        .where(models.Run.suite_id == run.suite_id, models.Run.id < run.id)
        .order_by(models.Run.id.desc())
        .limit(1)
    )
    current = {f.finding_key: f for f in run.findings}
    previous = {f.finding_key: f for f in (prev.findings if prev else [])}

    def info(f) -> dict:
        return {"finding_key": f.finding_key, "title": f.title, "severity": f.severity,
                "type": f.type, "page_url": f.page_url}

    new = [info(f) for k, f in current.items() if k not in previous]
    resolved = [info(f) for k, f in previous.items() if k not in current]
    persisting = [info(f) for k, f in current.items() if k in previous]
    return {"new": new, "resolved": resolved, "persisting": persisting}
