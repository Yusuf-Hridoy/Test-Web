"""qascan — full web UI (Phase 6.5).

Everything from the screen: paste a URL, tick checks, Run, watch live progress,
see results. Functional flow (targets / auth / generate / run) and history too.

The UI is a thin shell: it calls ``qascan.service`` only — no Playwright, no Gemini,
no SQL business logic here. Long scans run in a background thread so the UI never
freezes; progress is polled from a shared dict in session state. Transient UI state
lives in st.session_state; durable data goes through the DB.

Run with:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from qascan import service
from qascan.config import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PAGES,
    DEFAULT_TIME_BUDGET_SECONDS,
    RunLimits,
)
from qascan.db import models, repository
from qascan.db.session import get_sessionmaker
from qascan.functional.schema import Step, Suite, TargetConfig, TestCase

st.set_page_config(page_title="qascan", page_icon="🔎", layout="wide")

SUITES_DIR = Path("suites")
AUTH_DIR = Path("outputs/auth")
_SEV_ORDER = {"critical": 0, "warning": 1, "minor": 2, "info": 3}
_SEV_EMOJI = {"critical": "🔴", "warning": "🟠", "minor": "🔵", "info": "⚪"}
_STATUS_EMOJI = {"pass": "🟢", "healthy": "🟢", "fail": "🔴", "critical": "🔴",
                 "needs_review": "🟡", "warning": "🟠", "minor": "🔵",
                 "session_expired": "🟠", "flaky": "🟣", "cancelled": "⚪"}

CHECK_INFO = {
    "exploratory": ("Exploratory crawl", "Broken pages/links/images, console errors", True),
    "accessibility": ("Accessibility", "axe-core / WCAG violations", True),
    "seo": ("SEO basics", "Titles, meta, headings, canonical, sitemap", False),
    "performance": ("Performance", "Lighthouse — needs Node + lighthouse", False),
}


@st.cache_resource
def _sessionmaker():
    return get_sessionmaker()


def _session():
    return _sessionmaker()()


# --------------------------------------------------------------------------- #
# Background scan (never block the main thread)
# --------------------------------------------------------------------------- #
def _start_scan(url: str, checks: set[str], limits: RunLimits) -> None:
    state = {"status": "running", "pages": 0, "current": "", "url": url,
             "limits": limits, "cancel": threading.Event(), "outcome": None,
             "error": None, "started": time.time()}

    def worker():
        try:
            def prog(ev):
                state["pages"] = ev.get("pages", state["pages"])
                state["current"] = ev.get("current", "")
            state["outcome"] = service.run_scan(
                url, checks=checks, limits=limits, on_progress=prog, cancel=state["cancel"])
            state["status"] = "done"
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI, never a traceback dump
            state["error"] = f"{type(exc).__name__}: {exc}"
            state["status"] = "error"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    state["thread"] = t
    st.session_state["scan"] = state


def _render_scan_progress(state: dict) -> None:
    st.subheader(f"Scanning {state['url']}")
    elapsed = time.time() - state["started"]
    budget = state["limits"].time_budget_seconds
    c1, c2, c3 = st.columns(3)
    c1.metric("Pages scanned", state["pages"])
    c2.metric("Elapsed", f"{elapsed:.0f}s / {budget:.0f}s")
    c3.metric("Status", "running…")
    st.progress(min(state["pages"] / max(state["limits"].max_pages, 1), 1.0))
    st.caption(f"Current: {state['current'] or '…'}")
    if st.button("⏹ Cancel", type="secondary"):
        state["cancel"].set()
        st.toast("Cancelling at the next page boundary…")
    # Poll without blocking the engine: re-run this script shortly.
    time.sleep(0.8)
    st.rerun()


def _render_findings(findings) -> None:
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.check, []).append(f)
    for check in sorted(grouped):
        items = sorted(grouped[check], key=lambda f: _SEV_ORDER.get(
            f.severity.value if hasattr(f.severity, "value") else f.severity, 9))
        st.markdown(f"#### {check.title()} ({len(items)})")
        for f in items:
            sev = f.severity.value if hasattr(f.severity, "value") else f.severity
            with st.expander(f"{_SEV_EMOJI.get(sev, '⚪')} {f.title}  ·  {sev}"):
                st.write(f.detail)
                st.caption(f.page_url)
                if getattr(f, "evidence", None) and Path(f.evidence).exists():
                    st.image(f.evidence, width=420)


def _render_scan_results(state: dict) -> None:
    if state["status"] == "error":
        st.error(f"Scan failed: {state['error']}")
        if st.button("← New scan"):
            st.session_state["scan"] = None
            st.rerun()
        return

    outcome = state["outcome"]
    r = outcome.crawl
    counts = service.severity_counts(r.findings)
    verdict = ("Critical issues" if counts.get("critical") else
               "Needs attention" if counts.get("warning") else
               "Minor issues" if counts.get("minor") else "Healthy")
    st.subheader(f"Results — {state['url']}")
    if r.stopped_reason == "cancelled":
        st.warning("Scan cancelled — showing partial results.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pages scanned", r.pages_scanned)
    c2.metric("Issues", len(r.findings))
    c3.metric("Verdict", verdict)
    c4.metric("Duration", f"{r.duration_seconds}s")

    if outcome.diff:
        d = outcome.diff
        st.caption(f"vs previous run: 🆕 {len(d['new'])} new · ✅ {len(d['resolved'])} "
                   f"resolved · ➖ {len(d['persisting'])} persisting")
    if outcome.persist_error == "no_database":
        st.info("Not saved to history (no DATABASE_URL configured).")

    report = outcome.out_dir / "report.html"
    if report.exists():
        st.download_button("⬇ Download report.html", report.read_bytes(),
                           file_name="report.html", mime="text/html")

    if r.findings:
        _render_findings(r.findings)
    else:
        st.success("No issues found. 🎉")

    if st.button("← New scan"):
        st.session_state["scan"] = None
        st.rerun()


def screen_new_scan() -> None:
    st.header("🔎 New scan")
    scan = st.session_state.get("scan")
    if scan and scan["status"] == "running":
        _render_scan_progress(scan)
        return
    if scan and scan["status"] in ("done", "error"):
        _render_scan_results(scan)
        return

    url = st.text_input("URL to scan", placeholder="https://example.com")
    st.markdown("**Checks**")
    selected: set[str] = set()
    cols = st.columns(len(CHECK_INFO))
    for col, (key, (label, desc, default_on)) in zip(cols, CHECK_INFO.items(), strict=True):
        with col:
            if st.checkbox(label, value=default_on, key=f"chk_{key}"):
                selected.add(key)
            st.caption(f"{desc}  ·  _no setup_")

    with st.expander("Advanced limits"):
        mp = st.number_input("Max pages", 1, 1000, DEFAULT_MAX_PAGES)
        md = st.number_input("Max depth", 0, 20, DEFAULT_MAX_DEPTH)
        tb = st.number_input("Time budget (s)", 10, 3600, int(DEFAULT_TIME_BUDGET_SECONDS))

    with st.expander("Functional testing (optional — needs a saved suite)"):
        _functional_quick_run()

    can_run = bool(url.strip()) and bool(selected)
    if st.button("▶ Run scan", type="primary", disabled=not can_run):
        limits = RunLimits(max_pages=int(mp), max_depth=int(md), time_budget_seconds=float(tb))
        _start_scan(url.strip(), selected, limits)
        st.rerun()
    if not can_run:
        st.caption("Enter a URL and tick at least one check to enable Run.")


# --------------------------------------------------------------------------- #
# Functional
# --------------------------------------------------------------------------- #
def _saved_suites() -> list[Path]:
    SUITES_DIR.mkdir(exist_ok=True)
    return sorted(list(SUITES_DIR.glob("*.yaml")) + list(Path("examples").glob("*.yaml")))


def _run_suite_bg(path: Path) -> None:
    state = {"status": "running", "path": str(path), "outcome": None,
             "error": None, "started": time.time()}

    def worker():
        try:
            suite = Suite.from_file(path)
            state["outcome"] = service.run_functional(suite)
            state["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"{type(exc).__name__}: {exc}"
            state["status"] = "error"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    state["thread"] = t
    st.session_state["frun"] = state


def _functional_quick_run() -> None:
    suites = _saved_suites()
    if not suites:
        st.caption("No saved suites yet. Create one in the Functional tab.")
        return
    choice = st.selectbox("Saved suite", suites, format_func=lambda p: p.name,
                          key="quick_suite")
    if st.button("▶ Run suite", key="quick_run"):
        _run_suite_bg(choice)
        st.rerun()


def _render_functional_run(state: dict) -> None:
    if state["status"] == "running":
        st.info(f"Running {Path(state['path']).name}… ({time.time()-state['started']:.0f}s)")
        time.sleep(0.8)
        st.rerun()
        return
    if state["status"] == "error":
        st.error(f"Run failed: {state['error']}")
    else:
        r = state["outcome"].result
        if r.status == "session_expired":
            st.warning(f"Session expired — re-capture auth. {r.message}")
        else:
            st.success(f"Status: {r.status} · {r.llm_calls} LLM call(s)")
            for case in r.cases:
                emoji = _STATUS_EMOJI.get(case.status, "⚪")
                st.markdown(f"**{emoji} {case.name}** — {case.status}")
                rows = [{"#": s.index, "action": s.action, "status": s.status,
                         "detail": s.message} for s in case.steps]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if st.button("← Back"):
        st.session_state["frun"] = None
        st.rerun()


def screen_functional() -> None:
    st.header("🧪 Functional testing")
    frun = st.session_state.get("frun")
    if frun:
        _render_functional_run(frun)
        return

    tab_targets, tab_suites, tab_generate = st.tabs(
        ["Targets & auth", "Suites", "Generate from instruction"])

    with tab_targets:
        _tab_targets()
    with tab_suites:
        _tab_suites()
    with tab_generate:
        _tab_generate()


def _tab_targets() -> None:
    st.subheader("Targets")
    with _session() as s:
        for t in s.scalars(select(models.Target)).all():
            st.markdown(f"- **{t.label or t.base_url}** · `{t.base_url}` · "
                        f"auth: {t.auth_kind or 'none'}")
    with st.form("add_target"):
        base = st.text_input("Base URL", placeholder="https://app.example.com")
        label = st.text_input("Label", placeholder="My app (staging)")
        if st.form_submit_button("Add target") and base.strip():
            with _session() as s:
                repository.get_or_create_target(s, base.strip())
                if label.strip():
                    tgt = s.scalar(
                        select(models.Target).where(models.Target.base_url == base.strip()))
                    tgt.label = label.strip()
                s.commit()
            st.rerun()

    st.divider()
    st.subheader("Capture login (one-time, opens a real browser)")
    st.markdown(
        "Auth can't be a pure in-page click — a browser window must open so you can "
        "log in (MFA/SSO included). Run this **in the terminal where Streamlit is running**:")
    cap_url = st.text_input("Login URL", placeholder="https://app.example.com/login")
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUTH_DIR / "session.json"
    st.code(f"qascan auth capture {cap_url or '<URL>'} --out {out_path}", language="bash")
    if out_path.exists():
        st.success(f"Session captured at {out_path} — reference it in a suite's target.auth.")


def _tab_suites() -> None:
    st.subheader("Saved suites")
    suites = _saved_suites()
    if not suites:
        st.caption("None yet — generate one in the next tab.")
        return
    path = st.selectbox("Suite", suites, format_func=lambda p: p.name)
    suite = Suite.from_file(path)
    st.caption(f"{suite.name} · {len(suite.cases)} case(s) · target {suite.target.base_url}")
    for case in suite.cases:
        st.markdown(f"**{case.name}** ({case.source}) — {len(case.steps)} steps")
        st.json([s.model_dump(by_alias=True, exclude_none=True) for s in case.steps],
                expanded=False)
    if st.button("▶ Run this suite"):
        _run_suite_bg(path)
        st.rerun()


def _tab_generate() -> None:
    st.subheader("Generate a test from plain English (compile-once)")
    url = st.text_input("URL to explore", key="gen_url", placeholder="https://www.saucedemo.com")
    instruction = st.text_area("Describe what to test",
                               placeholder="log in as standard_user / secret_sauce and confirm "
                                           "the inventory list appears")
    if st.button("✨ Draft steps", disabled=not (url.strip() and instruction.strip())):
        with st.spinner("Exploring the page once and drafting…"):
            tmp = SUITES_DIR / "_draft.yaml"
            SUITES_DIR.mkdir(exist_ok=True)
            suite, _ = service.generate_suite(url.strip(), instruction.strip(), tmp)
        st.session_state["draft"] = suite.model_dump(by_alias=True, exclude_none=True)
        st.rerun()

    draft = st.session_state.get("draft")
    if not draft:
        return
    st.markdown("**Review & edit the drafted steps**, then save (saving freezes it — "
                "running never re-plans):")
    case = draft["cases"][0]
    edited = st.data_editor(pd.DataFrame(case["steps"]), num_rows="dynamic",
                            use_container_width=True, key="draft_editor")
    name = st.text_input("Save as", value=f"{draft['name']}.yaml")
    if st.button("💾 Save suite"):
        steps = [{k: v for k, v in row.items() if pd.notna(v) and v != ""}
                 for row in edited.to_dict("records")]
        saved = Suite(
            name=draft["name"],
            target=TargetConfig(base_url=draft["target"]["base_url"]),
            cases=[TestCase(id=case.get("id", draft["name"]), name=case["name"],
                            source="generated",
                            steps=[Step.model_validate(s) for s in steps])])
        out = SUITES_DIR / name
        out.write_text(saved.to_yaml(), encoding="utf-8")
        st.session_state["draft"] = None
        st.success(f"Saved {out}. Run it from the Suites tab (zero planning calls).")


# --------------------------------------------------------------------------- #
# History (the former dashboard)
# --------------------------------------------------------------------------- #
def screen_history() -> None:
    st.header("📊 History & review")
    sub = st.radio("View", ["Runs", "Healed selectors", "Schedules"], horizontal=True)
    if sub == "Runs":
        _history_runs()
    elif sub == "Healed selectors":
        _history_healed()
    else:
        _history_schedules()


def _history_runs() -> None:
    with _session() as s:
        suites = s.scalars(select(models.Suite).order_by(models.Suite.name)).all()
        if not suites:
            st.info("No runs yet. Start one from **New scan**.")
            return
        suite = st.selectbox("Suite", suites, format_func=lambda x: f"{x.name} ({x.kind})")
        runs = s.scalars(select(models.Run).where(models.Run.suite_id == suite.id)
                         .order_by(models.Run.id.desc())).all()
        if not runs:
            st.info("No runs for this suite.")
            return
        run = st.selectbox("Run", runs,
                           format_func=lambda r: f"#{r.id} · {r.status} · "
                                                 f"{r.started_at:%Y-%m-%d %H:%M}")
        c1, c2, c3 = st.columns(3)
        c1.metric("Status", run.status)
        c2.metric("LLM calls", run.llm_calls)
        c3.metric("Duration", f"{run.duration or 0}s")
        diff = repository.diff_findings(s, run)
        st.caption(f"🆕 {len(diff['new'])} new · ✅ {len(diff['resolved'])} resolved · "
                   f"➖ {len(diff['persisting'])} persisting (vs previous run)")
        findings = s.scalars(select(models.Finding).where(models.Finding.run_id == run.id)).all()
        if findings:
            rows = sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))
            st.dataframe(pd.DataFrame([
                {"severity": f.severity, "check": f.check, "type": f.type,
                 "title": f.title, "page": f.page_url} for f in rows]),
                use_container_width=True, hide_index=True)
        steps = s.scalars(select(models.StepResult).where(models.StepResult.run_id == run.id)
                          .order_by(models.StepResult.id)).all()
        if steps:
            st.markdown("**Steps** (🟡 needs-review where applicable):")
            st.dataframe(pd.DataFrame([
                {"#": st_.step_index, "status": st_.status, "detail": st_.message}
                for st_ in steps]), use_container_width=True, hide_index=True)


def _history_healed() -> None:
    st.caption("Selectors fixed by the LLM. Approve to trust, reject to force a re-heal.")
    with _session() as s:
        pending = s.scalars(select(models.SelectorCache)
                            .where(models.SelectorCache.reviewed.is_(False))
                            .order_by(models.SelectorCache.id)).all()
        if not pending:
            st.success("Nothing to review. 🎉")
            return
        for row in pending:
            suite = s.get(models.Suite, row.suite_id)
            with st.container(border=True):
                st.markdown(f"**{suite.name if suite else row.suite_id}** · step `{row.step_key}`")
                st.code(row.selector, language="text")
                a, r = st.columns(2)
                if a.button("✅ Approve", key=f"a{row.id}"):
                    repository.approve_heal(s, row)
                    s.commit()
                    st.rerun()
                if r.button("🔁 Reject (re-heal next run)", key=f"r{row.id}"):
                    repository.reject_heal(s, row, suite.name if suite else "")
                    s.commit()
                    st.rerun()


def _history_schedules() -> None:
    with _session() as s:
        suites = s.scalars(select(models.Suite).order_by(models.Suite.name)).all()
        with st.form("add_sched"):
            suite = st.selectbox("Suite", suites, format_func=lambda x: f"{x.name} ({x.kind})") \
                if suites else None
            cron = st.text_input("Cron (5-field)", value="*/30 * * * *")
            if st.form_submit_button("Add schedule") and suite:
                repository.add_schedule(s, suite.id, cron)
                s.commit()
                st.rerun()
        for sched in s.scalars(select(models.Schedule).order_by(models.Schedule.id)).all():
            suite = s.get(models.Suite, sched.suite_id)
            sname = suite.name if suite else sched.suite_id
            with st.container(border=True):
                st.markdown(f"**{sname}** · `{sched.cron_expr}` · "
                            f"{'🟢 enabled' if sched.enabled else '⚪ disabled'} · "
                            f"last {sched.last_run_at or '—'}")
                c1, c2, c3 = st.columns(3)
                if c1.button("Toggle", key=f"t{sched.id}"):
                    sched.enabled = not sched.enabled
                    s.commit()
                    st.rerun()
                if c2.button("Run now", key=f"rn{sched.id}"):
                    from qascan import scheduler
                    with st.spinner("Running…"):
                        st.session_state["sched_result"] = scheduler.execute_suite(sched.suite_id)
                    st.rerun()
                if c3.button("Delete", key=f"del{sched.id}"):
                    s.delete(sched)
                    s.commit()
                    st.rerun()
        if st.session_state.get("sched_result"):
            st.info(str(st.session_state.pop("sched_result")))


PAGES = {
    "🔎 New scan": screen_new_scan,
    "🧪 Functional": screen_functional,
    "📊 History": screen_history,
}

st.sidebar.title("🔎 qascan")
st.sidebar.caption("Web QA scanner + functional agent")
choice = st.sidebar.radio("Go to", list(PAGES), key="nav")
st.sidebar.caption("🟢 pass · 🔴 fail · 🟡 needs review · 🔵 info")

try:
    PAGES[choice]()
except OperationalError as exc:
    st.error("Cannot reach the database. Is Postgres running and DATABASE_URL set?\n\n"
             f"```\n{exc}\n```")
except Exception as exc:  # noqa: BLE001 — clean error state, never a raw traceback
    st.error(f"Something went wrong:\n\n```\n{exc}\n```")
