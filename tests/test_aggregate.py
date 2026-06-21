"""Findings-quality hardening — grouping/dedup, stable keys, severity mapping,
first-party/third-party partition, and diff reconciliation (Fixes 1-6)."""

from __future__ import annotations

import os

from qascan import aggregate
from qascan.checks.accessibility import _IMPACT_TO_SEVERITY
from qascan.config import RunLimits
from qascan.findings import Finding, Severity
from qascan.urls import normalize_message, strip_volatile


def _f(type_, sev, *, page, detail="", meta=None, third_party=False, check="exploratory"):
    return Finding.create(check=check, type=type_, severity=sev, title=type_,
                          detail=detail, page_url=page, key=detail or page,
                          meta=meta or {}, third_party=third_party)


# --------------------------------------------------------------------------- #
# Fix 2 — stable key despite volatile URL params
# --------------------------------------------------------------------------- #
def test_strip_volatile_params():
    a = strip_volatile("https://x.test/p?gtm=123&auid=1700000000&keep=1")
    b = strip_volatile("https://x.test/p?gtm=999&auid=1800000000&keep=1")
    assert a == b == "https://x.test/p?keep=1"


def test_normalize_message_collapses_cache_busters():
    m1 = normalize_message("CSP blocked https://clarity.ms/t?auid=1700000000001")
    m2 = normalize_message("CSP blocked https://clarity.ms/t?auid=1800000000002")
    assert m1 == m2


def test_grouping_signature_stable_across_volatile_links():
    g1 = aggregate.grouping_signature(_f("broken_link", Severity.WARNING, page="https://x.test/",
                                         meta={"url": "https://x.test/d?gtm=1"}))
    g2 = aggregate.grouping_signature(_f("broken_link", Severity.WARNING, page="https://x.test/",
                                         meta={"url": "https://x.test/d?gtm=2"}))
    assert g1 == g2


# --------------------------------------------------------------------------- #
# Fix 1 — grouping / dedup
# --------------------------------------------------------------------------- #
def test_group_collapses_repeated_console_error_across_pages():
    # Same Clarity CSP error firing 3x on each of 5 pages -> one grouped finding.
    findings = []
    for p in range(5):
        for _ in range(3):
            findings.append(_f("console_error_thirdparty", Severity.MINOR,
                               page=f"https://x.test/page{p}",
                               meta={"message": "Refused to connect to clarity.ms (CSP)",
                                     "source": "https://www.clarity.ms/s"},
                               third_party=True))
    grouped = aggregate.group(findings)
    assert len(grouped) == 1
    g = grouped[0]
    assert g.occurrences == 15
    assert len(g.pages) == 5
    assert g.third_party is True


def test_group_distinct_problems_stay_distinct():
    findings = [
        _f("broken_link", Severity.WARNING, page="https://x.test/a",
           meta={"url": "https://x.test/missing"}),
        _f("broken_image", Severity.WARNING, page="https://x.test/a",
           meta={"src": "https://x.test/logo.png"}),
        _f("wcag_violation", Severity.CRITICAL, page="https://x.test/a", check="accessibility",
           meta={"rule_id": "button-name", "impact": "critical"}),
    ]
    assert len(aggregate.group(findings)) == 3


# --------------------------------------------------------------------------- #
# Fix 3 — severity mapping reconciles with axe impact
# --------------------------------------------------------------------------- #
def test_severity_mapping_no_contradiction():
    assert _IMPACT_TO_SEVERITY["critical"] == Severity.CRITICAL
    assert _IMPACT_TO_SEVERITY["serious"] == Severity.WARNING
    assert _IMPACT_TO_SEVERITY["moderate"] == Severity.MINOR
    assert _IMPACT_TO_SEVERITY["minor"] == Severity.MINOR


# --------------------------------------------------------------------------- #
# Fix 4 — first-party actionable vs third-party / informational
# --------------------------------------------------------------------------- #
def test_partition_and_verdict():
    grouped = aggregate.group([
        _f("broken_link", Severity.WARNING, page="https://x.test/a",
           meta={"url": "https://x.test/missing"}),               # actionable
        _f("console_error_thirdparty", Severity.MINOR, page="https://x.test/a",
           meta={"message": "tracker boom", "source": "https://clarity.ms/x"},
           third_party=True),                                     # informational
        _f("link_blocked", Severity.INFO, page="https://x.test/a",
           meta={"url": "https://ext.test/x"}),                   # informational (info)
    ])
    actionable, informational = aggregate.partition(grouped)
    assert [f.type for f in actionable] == ["broken_link"]
    assert {f.type for f in informational} == {"console_error_thirdparty", "link_blocked"}
    assert aggregate.verdict(actionable) == ("Needs attention", "review")
    # A run with only third-party/info noise is Healthy (noise never drives verdict).
    assert aggregate.verdict([]) == ("Healthy", "pass")


# --------------------------------------------------------------------------- #
# Fix 2/6 — two unchanged scans: 0 new / 0 resolved, and the diff reconciles
# --------------------------------------------------------------------------- #
def test_diff_reconciles_on_grouped(http_server, tmp_path, db_session, monkeypatch):
    from qascan import service

    monkeypatch.setenv("DATABASE_URL", os.getenv("DATABASE_URL_TEST"))
    url = f"{http_server}/broken.html"
    limits = RunLimits(max_pages=1, max_depth=0)

    service.run_scan(url, limits=limits, out_root=tmp_path, persist=True)  # baseline
    second = service.run_scan(url, limits=limits, out_root=tmp_path, persist=True)

    d = second.diff
    # Unchanged page -> nothing new, nothing resolved (stable finding_key).
    assert d["new"] == []
    assert d["resolved"] == []
    # Diff reconciles: new + persisting == current distinct grouped total.
    distinct = len(second.crawl.findings)
    assert len(d["new"]) + len(d["persisting"]) == distinct
