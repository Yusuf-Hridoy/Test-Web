"""Phase 3 persistence — acceptance criteria 1, 3, 4, 5.

These run against the real Postgres test DB (DATABASE_URL_TEST); they skip if it
is not configured.
"""

from __future__ import annotations

from qascan.crawler import CrawlResult
from qascan.db import models, repository
from qascan.findings import Finding, Severity
from qascan.functional.executor import CaseResult, RunResult, StepResult
from qascan.functional.locators import HealCache, StepRef
from qascan.functional.schema import Step, Suite, TargetConfig, TestCase


def _finding(type_, page, detail, sev=Severity.WARNING):
    return Finding.create(
        check="exploratory", type=type_, severity=sev, title=type_,
        detail=detail, page_url=page, key=detail,
    )


# --------------------------------------------------------------------------- #
# Criterion 1: a scan writes a complete run with children
# --------------------------------------------------------------------------- #
def test_persist_scan_writes_run_and_findings(db_session):
    findings = [
        _finding("broken_link", "https://x.test/", "https://x.test/a", Severity.WARNING),
        _finding("page_error", "https://x.test/b", "500", Severity.CRITICAL),
    ]
    cr = CrawlResult(findings=findings, pages_scanned=5, stopped_reason="completed",
                     duration_seconds=1.2)
    run = repository.persist_scan(db_session, "https://x.test/home", cr)
    db_session.commit()

    assert run.id is not None
    assert run.pages_scanned == 5
    assert run.stopped_reason == "completed"
    assert run.status == "critical"  # worst severity present
    rows = db_session.query(models.Finding).filter_by(run_id=run.id).all()
    assert len(rows) == 2
    # finding_key is the stable Phase-1 hash.
    assert {r.finding_key for r in rows} == {f.id for f in findings}


# --------------------------------------------------------------------------- #
# Criterion 3: run-over-run diff (fix one, add one)
# --------------------------------------------------------------------------- #
def test_diff_new_resolved_persisting(db_session):
    url = "https://diff.test/"
    a = _finding("broken_link", url, "https://diff.test/a")
    b = _finding("broken_link", url, "https://diff.test/b")
    c = _finding("broken_link", url, "https://diff.test/c")

    repository.persist_scan(db_session, url, CrawlResult(findings=[a, b], pages_scanned=1,
                            stopped_reason="completed", duration_seconds=1.0))
    db_session.commit()
    # Run 2: fixed b, added c -> {a, c}
    run2 = repository.persist_scan(db_session, url, CrawlResult(findings=[a, c], pages_scanned=1,
                                   stopped_reason="completed", duration_seconds=1.0))
    db_session.commit()

    diff = repository.diff_findings(db_session, run2)
    assert [d["finding_key"] for d in diff["new"]] == [c.id]
    assert [d["finding_key"] for d in diff["resolved"]] == [b.id]
    assert [d["finding_key"] for d in diff["persisting"]] == [a.id]


# --------------------------------------------------------------------------- #
# Criterion 1 (functional) + 4 (heal review queue)
# --------------------------------------------------------------------------- #
def _functional_fixtures():
    suite = Suite(
        name="login-suite",
        target=TargetConfig(base_url="https://app.test"),
        cases=[TestCase(id="c1", name="login", steps=[
            Step(action="goto", target="/"),
            Step(action="click", selector="#go", hint="the button"),
        ])],
    )
    result = RunResult(
        suite_name="login-suite", base_url="https://app.test", status="needs_review",
        cases=[CaseResult(id="c1", name="login", source="written", status="pass",
                          duration_seconds=2.0, steps=[
            StepResult(index=0, action="goto", status="pass", message="ok"),
            StepResult(index=1, action="click", status="pass", healed=True, message="healed"),
        ])],
        healed_for_review=[{"step": "login-suite|c1|1", "selector": "#go-real",
                            "hint": "the button", "confidence": 0.9, "reviewed": False}],
        llm_calls=1, duration_seconds=2.0,
    )
    return suite, result


def test_persist_functional_writes_children(db_session):
    suite, result = _functional_fixtures()
    run = repository.persist_functional(db_session, suite, result)
    db_session.commit()

    assert run.status == "needs_review"
    assert run.llm_calls == 1 and run.llm_cost_estimate > 0
    assert db_session.query(models.TestCase).filter_by(suite_id=run.suite_id).count() == 1
    assert db_session.query(models.StepResult).filter_by(run_id=run.id).count() == 2
    sc = db_session.query(models.SelectorCache).filter_by(suite_id=run.suite_id).all()
    assert len(sc) == 1 and sc[0].reviewed is False  # flagged for review


def test_heal_approve_and_reject(db_session, tmp_path):
    suite, result = _functional_fixtures()
    run = repository.persist_functional(db_session, suite, result)
    db_session.commit()
    row = db_session.query(models.SelectorCache).filter_by(suite_id=run.suite_id).one()

    # Approve persists.
    repository.approve_heal(db_session, row)
    db_session.commit()
    assert db_session.get(models.SelectorCache, row.id).reviewed is True

    # Reject forces a re-heal: DB row gone AND runtime file cache entry removed.
    cache = HealCache("login-suite", root=tmp_path)
    ref = StepRef("login-suite", "c1", 1)
    cache.put(ref, "#go-real", "the button", 0.9)
    assert cache.get(ref) == "#go-real"

    repository.reject_heal(db_session, row, "login-suite", cache_root=tmp_path)
    db_session.commit()
    assert db_session.get(models.SelectorCache, row.id) is None
    assert HealCache("login-suite", root=tmp_path).get(ref) is None  # re-heals next run


# --------------------------------------------------------------------------- #
# Criterion 5: alembic upgrade head builds the schema from scratch
# --------------------------------------------------------------------------- #
def test_alembic_upgrade_head_from_scratch(monkeypatch):
    import os

    from dotenv import load_dotenv

    load_dotenv()
    url = os.getenv("DATABASE_URL_TEST", "").replace("/qascan_test", "/qascan_alembic_test")
    if "qascan_alembic_test" not in url:
        import pytest
        pytest.skip("alembic test DB not configured")

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    # Start from a truly empty schema.
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert {"targets", "suites", "test_cases", "runs", "step_results",
            "findings", "selector_cache"}.issubset(tables)
