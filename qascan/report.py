"""Report writer: machine-readable ``results.json`` + shareable ``report.html``.

Both land in ``outputs/<domain>-<timestamp>/``. The HTML is fully self-contained
(inline CSS, no external requests) so it can be shared as a single file.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .crawler import CrawlResult
from .findings import Finding, Severity

if TYPE_CHECKING:  # avoid a runtime import cycle with functional.executor
    from .functional.executor import RunResult

_SEVERITY_LABEL = {
    Severity.CRITICAL: "Critical",
    Severity.WARNING: "Warning",
    Severity.MINOR: "Minor",
    Severity.INFO: "Info",
}
_SEVERITY_COLOR = {
    Severity.CRITICAL: "#dc2626",
    Severity.WARNING: "#d97706",
    Severity.MINOR: "#2563eb",
    Severity.INFO: "#6b7280",
}


def _slug(url: str) -> str:
    host = urlparse(url).hostname or "scan"
    return host.replace(":", "_")


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = Counter(f.severity.value for f in findings)
    return {s.value: counts.get(s.value, 0) for s in Severity}


def _verdict(counts: dict[str, int]) -> tuple[str, str]:
    if counts["critical"] > 0:
        return "Critical issues found", _SEVERITY_COLOR[Severity.CRITICAL]
    if counts["warning"] > 0:
        return "Needs attention", _SEVERITY_COLOR[Severity.WARNING]
    if counts["minor"] > 0:
        return "Minor issues", _SEVERITY_COLOR[Severity.MINOR]
    return "Healthy", "#16a34a"


def build_meta(url: str, result: CrawlResult) -> dict:
    return {
        "url": url,
        "generated_at": datetime.now(UTC).isoformat(),
        "pages_scanned": result.pages_scanned,
        "stopped_reason": result.stopped_reason,
        "duration_seconds": result.duration_seconds,
        "counts_by_severity": severity_counts(result.findings),
        "total_findings": len(result.findings),
    }


def write_report(url: str, result: CrawlResult, out_root: Path | str = "outputs") -> Path:
    """Write both artifacts and return the output directory."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(out_root) / f"{_slug(url)}-{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = build_meta(url, result)
    findings_sorted = sorted(result.findings, key=lambda f: (f.severity.rank, f.check, f.type))

    (out_dir / "results.json").write_text(
        json.dumps(
            {"meta": meta, "findings": [f.model_dump(mode="json") for f in findings_sorted]},
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "report.html").write_text(
        _render_html(meta, findings_sorted), encoding="utf-8"
    )
    return out_dir


def _card(label: str, value: str, color: str = "#111827") -> str:
    return (
        '<div class="card">'
        f'<div class="card-value" style="color:{color}">{html.escape(value)}</div>'
        f'<div class="card-label">{html.escape(label)}</div>'
        "</div>"
    )


def _render_html(meta: dict, findings: list[Finding]) -> str:
    counts = meta["counts_by_severity"]
    verdict_text, verdict_color = _verdict(counts)

    cards = "".join(
        [
            _card("Pages scanned", str(meta["pages_scanned"])),
            _card("Total issues", str(meta["total_findings"])),
            _card("Critical", str(counts["critical"]), _SEVERITY_COLOR[Severity.CRITICAL]),
            _card("Warnings", str(counts["warning"]), _SEVERITY_COLOR[Severity.WARNING]),
            _card("Minor", str(counts["minor"]), _SEVERITY_COLOR[Severity.MINOR]),
        ]
    )

    grouped: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:  # already severity-sorted; preserve within each group
        grouped[f.check].append(f)

    sections = []
    for check in sorted(grouped):
        rows = []
        for f in grouped[check]:
            color = _SEVERITY_COLOR[f.severity]
            rows.append(
                '<tr>'
                f'<td><span class="badge" style="background:{color}">'
                f"{html.escape(_SEVERITY_LABEL[f.severity])}</span></td>"
                f'<td><div class="title">{html.escape(f.title)}</div>'
                f'<div class="detail">{html.escape(f.detail)}</div></td>'
                f'<td class="page"><a href="{html.escape(f.page_url)}">'
                f"{html.escape(f.page_url)}</a></td>"
                "</tr>"
            )
        body = "".join(rows) or '<tr><td colspan="3" class="empty">No issues.</td></tr>'
        sections.append(
            f'<section><h2>{html.escape(check.title())} '
            f'<span class="count">({len(grouped[check])})</span></h2>'
            '<table><thead><tr><th>Severity</th><th>Issue</th><th>Page</th></tr></thead>'
            f"<tbody>{body}</tbody></table></section>"
        )
    sections_html = "".join(sections) or '<p class="empty">No findings recorded.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>qascan report — {html.escape(meta["url"])}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; line-height:1.5; background:#f3f4f6; color:#111827;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }}
  header h1 {{ margin:0 0 4px; font-size: 1.5rem; }}
  header .url {{ color:#374151; font-size:.95rem; word-break:break-all; }}
  .verdict {{ display:inline-block; margin-top:14px; padding:6px 14px; border-radius:999px;
             color:#fff; font-weight:600; background:{verdict_color}; }}
  .meta {{ color:#6b7280; font-size:.85rem; margin-top:10px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:14px; margin:26px 0; }}
  .card {{ flex:1 1 140px; background:#fff; border:1px solid #e5e7eb; border-radius:12px;
          padding:18px; text-align:center; }}
  .card-value {{ font-size:1.8rem; font-weight:700; }}
  .card-label {{ font-size:.8rem; color:#6b7280; text-transform:uppercase; letter-spacing:.04em; }}
  section {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; margin-bottom:22px;
            overflow:hidden; }}
  section h2 {{ margin:0; padding:16px 20px; font-size:1.05rem; border-bottom:1px solid #e5e7eb; }}
  section h2 .count {{ color:#9ca3af; font-weight:400; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:12px 20px; vertical-align:top; font-size:.9rem; }}
  th {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; color:#6b7280;
       background:#fafafa; }}
  tbody tr {{ border-top:1px solid #f3f4f6; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:999px; color:#fff;
           font-size:.72rem; font-weight:600; white-space:nowrap; }}
  .title {{ font-weight:600; }}
  .detail {{ color:#4b5563; font-size:.83rem; margin-top:2px; word-break:break-word; }}
  td.page {{ max-width:220px; word-break:break-all; font-size:.8rem; }}
  td.page a {{ color:#2563eb; text-decoration:none; }}
  .empty {{ color:#9ca3af; text-align:center; padding:24px; }}
  footer {{ text-align:center; color:#9ca3af; font-size:.8rem; margin-top:30px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>QA Scan Report</h1>
    <div class="url">{html.escape(meta["url"])}</div>
    <div class="verdict">{html.escape(verdict_text)}</div>
    <div class="meta">Scanned {meta["pages_scanned"]} page(s) in
      {meta["duration_seconds"]}s · stopped: {html.escape(meta["stopped_reason"])} ·
      {html.escape(meta["generated_at"])}</div>
  </header>
  <div class="cards">{cards}</div>
  {sections_html}
  <footer>Generated by qascan · oracle-free scan (exploratory + accessibility)</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Functional run report (Phase 2) — reuses the Phase 1 look & feel.
# --------------------------------------------------------------------------- #
_STATUS_COLOR = {
    "pass": "#16a34a",
    "fail": "#dc2626",
    "needs_review": "#d97706",
    "skipped": "#9ca3af",
    "session_expired": "#dc2626",
}


def _status_badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "#6b7280")
    label = status.replace("_", " ")
    return f'<span class="badge" style="background:{color}">{html.escape(label)}</span>'


def write_functional_report(result: RunResult, out_dir: Path | str) -> Path:
    """Write ``results.json`` + ``report.html`` for a functional run."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    (out_dir / "report.html").write_text(_render_functional_html(result), encoding="utf-8")
    return out_dir


def _render_functional_html(result: RunResult) -> str:
    total = len(result.cases)
    passed = sum(1 for c in result.cases if c.status == "pass")
    failed = sum(1 for c in result.cases if c.status == "fail")
    review = sum(1 for c in result.cases if c.status == "needs_review")

    cards = "".join(
        [
            _card("Cases", str(total)),
            _card("Passed", str(passed), _STATUS_COLOR["pass"]),
            _card("Failed", str(failed), _STATUS_COLOR["fail"]),
            _card("Needs review", str(review), _STATUS_COLOR["needs_review"]),
            _card("LLM calls", str(result.llm_calls)),
        ]
    )

    banner = ""
    if result.status == "session_expired":
        banner = (
            '<div class="banner">⚠ Session expired — suite was not run. '
            f"{html.escape(result.message)}</div>"
        )

    heal_html = ""
    if result.healed_for_review:
        rows = "".join(
            f"<tr><td>{html.escape(h['step'])}</td>"
            f"<td><code>{html.escape(str(h.get('selector', '')))}</code></td>"
            f"<td>{html.escape(str(h.get('hint', '')))}</td>"
            f"<td>{h.get('confidence', '')}</td></tr>"
            for h in result.healed_for_review
        )
        heal_html = (
            '<section><h2>Healed selectors '
            '<span class="count">(needs review)</span></h2>'
            "<table><thead><tr><th>Step</th><th>Healed selector</th><th>Hint</th>"
            f"<th>Conf.</th></tr></thead><tbody>{rows}</tbody></table></section>"
        )

    case_sections = []
    for c in result.cases:
        rows = "".join(
            "<tr>"
            f"<td>{s.index}</td><td>{html.escape(s.action)}</td>"
            f"<td>{_status_badge(s.status)}{' 🔧' if s.healed else ''}</td>"
            f'<td class="detail">{html.escape(s.message)}'
            f"{f' (conf {s.confidence})' if s.confidence is not None else ''}</td>"
            "</tr>"
            for s in c.steps
        )
        tag = "generated" if c.source == "generated" else "written"
        case_sections.append(
            f"<section><h2>{html.escape(c.name)} {_status_badge(c.status)} "
            f'<span class="count">· {html.escape(tag)} · {c.duration_seconds}s</span></h2>'
            "<table><thead><tr><th>#</th><th>Action</th><th>Status</th><th>Detail</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></section>"
        )
    cases_html = "".join(case_sections) or '<p class="empty">No cases run.</p>'
    verdict_color = _STATUS_COLOR.get(result.status, "#6b7280")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>qascan functional report — {html.escape(result.suite_name)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; line-height:1.5; background:#f3f4f6; color:#111827;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }}
  header h1 {{ margin:0 0 4px; font-size: 1.5rem; }}
  header .url {{ color:#374151; font-size:.95rem; word-break:break-all; }}
  .verdict {{ display:inline-block; margin-top:14px; padding:6px 14px; border-radius:999px;
             color:#fff; font-weight:600; background:{verdict_color}; }}
  .meta {{ color:#6b7280; font-size:.85rem; margin-top:10px; }}
  .banner {{ background:#fef2f2; border:1px solid #fecaca; color:#991b1b; padding:14px 18px;
            border-radius:12px; margin:20px 0; font-weight:600; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:14px; margin:26px 0; }}
  .card {{ flex:1 1 130px; background:#fff; border:1px solid #e5e7eb; border-radius:12px;
          padding:18px; text-align:center; }}
  .card-value {{ font-size:1.8rem; font-weight:700; }}
  .card-label {{ font-size:.8rem; color:#6b7280; text-transform:uppercase; letter-spacing:.04em; }}
  section {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; margin-bottom:22px;
            overflow:hidden; }}
  section h2 {{ margin:0; padding:16px 20px; font-size:1.05rem; border-bottom:1px solid #e5e7eb; }}
  section h2 .count {{ color:#9ca3af; font-weight:400; font-size:.85rem; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:10px 20px; vertical-align:top; font-size:.9rem; }}
  th {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; color:#6b7280;
       background:#fafafa; }}
  tbody tr {{ border-top:1px solid #f3f4f6; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:999px; color:#fff;
           font-size:.72rem; font-weight:600; white-space:nowrap; }}
  .detail {{ color:#4b5563; font-size:.85rem; word-break:break-word; }}
  code {{ background:#f3f4f6; padding:1px 5px; border-radius:4px; font-size:.82rem; }}
  .empty {{ color:#9ca3af; text-align:center; padding:24px; }}
  footer {{ text-align:center; color:#9ca3af; font-size:.8rem; margin-top:30px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Functional Run — {html.escape(result.suite_name)}</h1>
    <div class="url">{html.escape(result.base_url)}</div>
    <div class="verdict">{html.escape(result.status.replace("_", " "))}</div>
    <div class="meta">{passed}/{total} case(s) passed · {result.llm_calls} LLM call(s) ·
      {result.duration_seconds}s · trace.zip + evidence/ alongside this file</div>
  </header>
  {banner}
  <div class="cards">{cards}</div>
  {heal_html}
  {cases_html}
  <footer>Generated by qascan · functional engine (deterministic-first, LLM-fallback)</footer>
</div>
</body>
</html>
"""
