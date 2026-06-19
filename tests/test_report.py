"""Report artifacts + id stability across runs (criteria 1 and 6)."""

from __future__ import annotations

import json

from qascan.config import RunLimits
from qascan.crawler import crawl
from qascan.findings import Finding
from qascan.report import write_report


async def test_write_report_produces_both_artifacts(tmp_path, http_server):
    result = await crawl(f"{http_server}/broken.html", RunLimits(max_pages=5, max_depth=1))
    out_dir = write_report(f"{http_server}/broken.html", result, out_root=tmp_path)

    html_path = out_dir / "report.html"
    json_path = out_dir / "results.json"
    assert html_path.exists() and json_path.exists()

    # report.html is self-contained: no external http(s) resource references.
    html = html_path.read_text()
    assert "<style>" in html
    assert "http://cdn" not in html and "https://cdn" not in html

    # results.json is valid, re-loadable, and findings round-trip into the model.
    data = json.loads(json_path.read_text())
    assert "meta" in data and "findings" in data
    assert data["meta"]["pages_scanned"] == result.pages_scanned
    for raw in data["findings"]:
        Finding.model_validate(raw)


async def test_finding_ids_stable_across_two_runs(http_server):
    limits = RunLimits(max_pages=1, max_depth=0)
    r1 = await crawl(f"{http_server}/broken.html", limits)
    r2 = await crawl(f"{http_server}/broken.html", limits)
    assert {f.id for f in r1.findings} == {f.id for f in r2.findings}
    assert len(r1.findings) > 0
