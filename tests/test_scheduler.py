"""Phase 4 scheduler — overlap guard (crit 3), flaky retry (crit 4), and schedule
persistence across a worker restart (crit 1)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from qascan import scheduler
from qascan.functional.executor import CaseResult, RunResult, StepResult
from qascan.functional.schema import Step, Suite, TargetConfig, TestCase


# --------------------------------------------------------------------------- #
# Criterion 3: overlap prevention
# --------------------------------------------------------------------------- #
def test_suite_lock_prevents_overlap(db_engine):
    with scheduler.suite_lock(db_engine, 4242) as first:
        assert first is True
        with scheduler.suite_lock(db_engine, 4242) as second:
            assert second is False  # already locked -> overlap denied
    # Lock released after the context exits.
    with scheduler.suite_lock(db_engine, 4242) as third:
        assert third is True


# --------------------------------------------------------------------------- #
# Criterion 4: flaky vs broken
# --------------------------------------------------------------------------- #
def _suite():
    return Suite(
        name="retry-suite", target=TargetConfig(base_url="https://x.test"),
        cases=[
            TestCase(id="c1", name="c1", steps=[Step(action="click", selector="#a", hint="a")]),
            TestCase(id="c2", name="c2", steps=[Step(action="click", selector="#b", hint="b")]),
        ],
    )


def _result(statuses: dict[str, str]) -> RunResult:
    cases = [
        CaseResult(id=cid, name=cid, source="written", status=stat, duration_seconds=0.1,
                   steps=[StepResult(index=0, action="click",
                                     status="fail" if stat == "fail" else "pass", message="")])
        for cid, stat in statuses.items()
    ]
    status = "fail" if "fail" in statuses.values() else "pass"
    return RunResult(suite_name="retry-suite", base_url="https://x.test", status=status,
                     cases=cases, duration_seconds=0.1)


async def test_flaky_when_passes_on_retry(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_run_suite(suite, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # full run: c1 fails, c2 passes
            return _result({"c1": "fail", "c2": "pass"}), tmp_path
        return _result({"c1": "pass"}), tmp_path  # retry of c1 passes

    monkeypatch.setattr(scheduler, "run_suite", fake_run_suite)
    result, _out, flaky = await scheduler.run_with_retry(_suite(), out_root=tmp_path)

    assert flaky == {"c1"}
    by_id = {c.id: c.status for c in result.cases}
    assert by_id["c1"] == "flaky" and by_id["c2"] == "pass"
    assert result.status == "flaky"  # no real failure -> not a "fail"


async def test_consistent_failure_is_not_flaky(monkeypatch, tmp_path):
    async def fake_run_suite(suite, **kw):
        return _result({"c1": "fail", "c2": "pass"}), tmp_path  # fails both times

    monkeypatch.setattr(scheduler, "run_suite", fake_run_suite)
    result, _out, flaky = await scheduler.run_with_retry(_suite(), out_root=tmp_path)

    assert flaky == set()
    assert result.status == "fail"
    assert {c.id: c.status for c in result.cases}["c1"] == "fail"


# --------------------------------------------------------------------------- #
# Criterion 1: schedules survive a worker restart (persistent job store)
# --------------------------------------------------------------------------- #
def test_schedules_survive_restart(db_session, db_engine):
    from sqlalchemy import text

    from qascan.db import repository

    # Clean the APScheduler job table (not managed by our metadata).
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS apscheduler_jobs"))

    load_dotenv()
    url = os.getenv("DATABASE_URL_TEST")

    # A suite + an enabled schedule.
    target = repository.get_or_create_target(db_session, "https://sched.test")
    suite = repository.get_or_create_suite(db_session, target, "sched-suite", "scan")
    repository.add_schedule(db_session, suite.id, "*/5 * * * *")
    db_session.commit()

    sched1 = scheduler.build_scheduler(url=url)
    sched1.start(paused=True)  # paused: register jobs without firing them
    scheduler.sync_schedules(sched1, db_session)
    assert sched1.get_job(f"suite:{suite.id}") is not None
    sched1.shutdown(wait=False)

    # "Restart": a brand-new scheduler reading the same persistent store.
    sched2 = scheduler.build_scheduler(url=url)
    sched2.start(paused=True)
    try:
        assert sched2.get_job(f"suite:{suite.id}") is not None  # survived
    finally:
        sched2.shutdown(wait=False)
