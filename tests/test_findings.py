"""Finding model: deterministic ids, JSON round-trip (criterion 6)."""

from __future__ import annotations

import json

from qascan.findings import Finding, Severity


def _make() -> Finding:
    return Finding.create(
        check="exploratory",
        type="broken_link",
        severity=Severity.WARNING,
        title="Broken link",
        detail="Link returned HTTP 404: https://x.test/missing",
        page_url="https://x.test/",
        key="https://x.test/missing",
    )


def test_id_is_stable_across_construction():
    assert _make().id == _make().id


def test_id_changes_with_key():
    a = _make()
    b = Finding.create(
        check="exploratory",
        type="broken_link",
        severity=Severity.WARNING,
        title="Broken link",
        detail="other",
        page_url="https://x.test/",
        key="https://x.test/other",
    )
    assert a.id != b.id


def test_id_ignores_detail_when_key_given():
    a = _make()
    b = Finding.create(
        check="exploratory",
        type="broken_link",
        severity=Severity.WARNING,
        title="Broken link",
        detail="completely different detail text",
        page_url="https://x.test/",
        key="https://x.test/missing",
    )
    assert a.id == b.id


def test_json_round_trip():
    f = _make()
    dumped = json.dumps(f.model_dump(mode="json"))
    loaded = Finding.model_validate_json(dumped)
    assert loaded == f


def test_severity_rank_order():
    ranks = [Severity.CRITICAL.rank, Severity.WARNING.rank, Severity.MINOR.rank, Severity.INFO.rank]
    assert ranks == sorted(ranks)
