"""Phase 4 alerting — criterion 2 (alert on new critical/regression, not on
unchanged) plus the pure decision logic."""

from __future__ import annotations

from qascan import notify
from qascan.crawler import CrawlResult
from qascan.db import models, repository
from qascan.findings import Finding, Severity


def _finding(type_, page, detail, sev=Severity.WARNING):
    return Finding.create(check="exploratory", type=type_, severity=sev, title=type_,
                          detail=detail, page_url=page, key=detail)


def _ctx(**kw):
    base = dict(suite_name="s", run_id=1, kind="scan", status="warning",
                new_findings=[], new_critical=[], failed_cases=[])
    base.update(kw)
    return notify.AlertContext(**base)


def test_should_notify_rules():
    crit = {"severity": "critical", "title": "x", "page_url": "u"}
    cfg = models.NotifyConfig(channel="slack", target="t", on_critical=True,
                              on_regression=True, on_failure=True)
    assert notify.should_notify(cfg, _ctx(new_critical=[crit], new_findings=[crit]))
    assert notify.should_notify(cfg, _ctx(failed_cases=["login"]))
    # Nothing new -> no alert, even though there may be persisting issues.
    assert not notify.should_notify(cfg, _ctx())


def test_alert_on_new_critical_then_silent_on_unchanged(db_session):
    url = "https://alert.test/"
    crit = _finding("page_error", url, "500", Severity.CRITICAL)

    run1 = repository.persist_scan(db_session, url, CrawlResult(
        findings=[crit], pages_scanned=1, stopped_reason="completed", duration_seconds=1.0))
    db_session.flush()
    db_session.add(models.NotifyConfig(suite_id=run1.suite_id, channel="slack",
                                       target="https://hooks.example/x"))
    db_session.flush()

    calls: list = []
    sender = lambda cfg, ctx, msg: calls.append((cfg.channel, msg))  # noqa: E731

    diff1 = repository.diff_findings(db_session, run1)
    ctx1 = notify.build_context("alert", run1, diff1, kind="scan", failed_cases=[])
    notify.notify_for_run(db_session, run1.suite_id, ctx1, sender=sender)
    assert len(calls) == 1 and "critical" in calls[0][1].lower()  # NEW critical -> alert

    # Identical second run: the critical persists but is not new -> no alert.
    run2 = repository.persist_scan(db_session, url, CrawlResult(
        findings=[crit], pages_scanned=1, stopped_reason="completed", duration_seconds=1.0))
    db_session.flush()
    calls.clear()
    diff2 = repository.diff_findings(db_session, run2)
    ctx2 = notify.build_context("alert", run2, diff2, kind="scan", failed_cases=[])
    notify.notify_for_run(db_session, run2.suite_id, ctx2, sender=sender)
    assert calls == []  # unchanged known issue -> silent


def test_one_failing_channel_does_not_block_others(db_session):
    url = "https://multi.test/"
    crit = _finding("page_error", url, "500", Severity.CRITICAL)
    run = repository.persist_scan(db_session, url, CrawlResult(
        findings=[crit], pages_scanned=1, stopped_reason="completed", duration_seconds=1.0))
    db_session.flush()
    db_session.add(models.NotifyConfig(suite_id=run.suite_id, channel="slack", target="bad"))
    db_session.add(models.NotifyConfig(suite_id=run.suite_id, channel="webhook", target="ok"))
    db_session.flush()

    def sender(cfg, ctx, msg):
        if cfg.channel == "slack":
            raise RuntimeError("slack down")

    diff = repository.diff_findings(db_session, run)
    ctx = notify.build_context("multi", run, diff, kind="scan", failed_cases=[])
    sent = notify.notify_for_run(db_session, run.suite_id, ctx, sender=sender)
    assert any("error" in s for s in sent) and any(s.get("channel") == "webhook" for s in sent)
