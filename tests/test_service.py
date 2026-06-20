"""Phase 6.5 service layer — the shared entry point for CLI and UI.

Covers parity (service == engine), cancel, progress, persistence, and the
generate->save->run zero-planning path (criteria 2, 4, 6).
"""

from __future__ import annotations

import os
import threading

from conftest import FakeLLM

from qascan import llm, service
from qascan.config import RunLimits
from qascan.crawler import crawl


async def _direct_findings(url):
    r = await crawl(url, RunLimits(max_pages=1, max_depth=0), checks={"exploratory", "seo"})
    return sorted(f.id for f in r.findings)


def test_service_run_scan_matches_engine(http_server, tmp_path):
    # The UI and CLI both go through service.run_scan; it must not alter results.
    import asyncio

    outcome = service.run_scan(
        f"{http_server}/broken.html", checks={"exploratory", "seo"},
        limits=RunLimits(max_pages=1, max_depth=0), out_root=tmp_path, persist=False)
    service_ids = sorted(f.id for f in outcome.crawl.findings)
    direct_ids = asyncio.run(_direct_findings(f"{http_server}/broken.html"))
    assert service_ids == direct_ids
    assert (outcome.out_dir / "report.html").exists()


def test_service_run_scan_cancel(http_server, tmp_path):
    cancel = threading.Event()
    cancel.set()  # cancelled before it starts -> stops at the first boundary
    outcome = service.run_scan(
        f"{http_server}/index.html", limits=RunLimits(max_pages=50, max_depth=3),
        out_root=tmp_path, persist=False, cancel=cancel)
    assert outcome.crawl.stopped_reason == "cancelled"
    assert outcome.crawl.pages_scanned == 0


def test_service_run_scan_progress(http_server, tmp_path):
    events = []
    service.run_scan(f"{http_server}/index.html", checks={"exploratory"},
                     limits=RunLimits(max_pages=3, max_depth=1), out_root=tmp_path,
                     persist=False, on_progress=lambda ev: events.append(ev))
    assert events and all("pages" in e for e in events)
    assert events[-1]["pages"] >= 1


def test_service_run_scan_persists(http_server, tmp_path, db_session, monkeypatch):
    # Point the service's session at the test DB (schema created by db_session).
    monkeypatch.setenv("DATABASE_URL", os.getenv("DATABASE_URL_TEST"))
    outcome = service.run_scan(f"{http_server}/broken.html",
                               limits=RunLimits(max_pages=1, max_depth=0),
                               out_root=tmp_path, persist=True)
    assert outcome.run_id is not None
    assert outcome.diff is not None and "new" in outcome.diff


def test_service_generate_then_run_zero_planning(http_server, tmp_path, monkeypatch):
    drafted = {"name": "gen", "steps": [
        {"action": "goto", "target": "/form.html"},
        {"action": "fill", "selector": "#name", "value": "Ada", "hint": "name"},
        {"action": "click", "selector": "#go", "hint": "continue"},
        {"action": "wait"},
        {"action": "expect", "assert": "url", "value": "welcome"},
    ]}
    fake = FakeLLM(lambda p, s: drafted)
    monkeypatch.setattr(llm, "_invoke", fake)

    out = tmp_path / "gen.yaml"
    suite, path = service.generate_suite(f"{http_server}/form.html", "sign in", out)
    assert path.exists() and suite.cases[0].source == "generated"
    assert len(fake.calls) == 1  # one planning call

    fake.calls.clear()
    from qascan.functional.schema import Suite
    outcome = service.run_functional(Suite.from_file(out), out_root=tmp_path, persist=False)
    assert outcome.result.status == "pass"
    assert fake.calls == []  # running never re-plans (criterion 4)
