"""qascan — web UI (Phase 6.5 + redesign).

A thin shell over ``qascan.service`` — no Playwright, no Gemini, no SQL/ORM here.
Long scans run in a background thread so the UI never freezes; progress is polled
from a shared dict in session state. All visual styling comes from ``theme.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import theme

from qascan import service
from qascan.config import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PAGES,
    DEFAULT_TIME_BUDGET_SECONDS,
    RunLimits,
)
from qascan.functional.schema import Step, Suite, TargetConfig, TestCase

log = logging.getLogger("qascan.ui")
st.set_page_config(page_title="qascan", page_icon="◆", layout="wide")

SUITES_DIR = Path("suites")
AUTH_DIR = Path("outputs/auth")
_SEV_ORDER = {"critical": 0, "fail": 0, "warning": 1, "review": 1, "minor": 2, "info": 3}

CHECK_INFO = {
    "exploratory": ("Exploratory crawl", "Broken links, images, console errors", True, False),
    "accessibility": ("Accessibility", "axe-core / WCAG violations", True, False),
    "seo": ("SEO basics", "Titles, meta, headings, canonical, sitemap", False, False),
    "performance": ("Performance", "Lighthouse — needs Node + lighthouse", False, True),
}


def _sev(f) -> str:
    s = f.severity
    return s.value if hasattr(s, "value") else s


# --------------------------------------------------------------------------- #
# Background scan
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
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI, not as a traceback
            log.exception("scan failed")
            state["error"] = f"{type(exc).__name__}: {exc}"
            state["status"] = "error"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    state["thread"] = t
    st.session_state["scan"] = state


def _render_scan_progress(state: dict) -> None:
    elapsed = time.time() - state["started"]
    budget = state["limits"].time_budget_seconds
    with st.status(f"Scanning  {state['url']}", state="running", expanded=True):
        st.progress(min(state["pages"] / max(state["limits"].max_pages, 1), 1.0))
        c1, c2 = st.columns(2)
        c1.markdown(f"**{state['pages']}** pages scanned")
        c2.markdown(f"**{elapsed:.0f}s** / {budget:.0f}s budget")
        st.markdown("Current: " + theme.mono(state["current"] or "starting…"),
                    unsafe_allow_html=True)
    if st.button("Cancel scan", type="secondary"):
        state["cancel"].set()
        st.toast("Cancelling at the next page boundary…")
    time.sleep(0.8)
    st.rerun()


def _render_findings(findings, diff: dict | None = None) -> None:
    new_keys = {d.get("finding_key") for d in (diff or {}).get("new", [])}
    grouped: dict[str, list] = {}
    for f in findings:
        grouped.setdefault(f.check, []).append(f)
    for check in sorted(grouped):
        items = sorted(grouped[check], key=lambda f: _SEV_ORDER.get(_sev(f), 9))
        st.markdown(f"##### {check.title()} · {len(items)}")
        for f in items:
            tags = []
            if getattr(f, "finding_key", None) in new_keys:
                tags.append(theme.badge("new", "review"))
            st.markdown(theme.finding_card(_sev(f), f.title, f.detail, f.page_url, tags),
                        unsafe_allow_html=True)
            ev = getattr(f, "evidence", None) or getattr(f, "evidence_path", None)
            if ev and Path(ev).exists():
                with st.expander("Evidence"):
                    st.image(ev, width=440)


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
    verdict, vkind = (("Critical issues", "fail") if counts.get("critical") else
                      ("Needs attention", "review") if counts.get("warning") else
                      ("Minor issues", "info") if counts.get("minor") else ("Healthy", "pass"))
    theme.section_header("Results", state["url"])
    if r.stopped_reason == "cancelled":
        st.warning("Scan cancelled — showing partial results.")

    cols = st.columns(4)
    cards = [("Pages scanned", str(r.pages_scanned), None),
             ("Issues", str(len(r.findings)), None),
             ("Verdict", verdict, vkind),
             ("Duration", f"{r.duration_seconds}s", None)]
    for col, (label, value, kind) in zip(cols, cards, strict=True):
        col.markdown(theme.metric_card(label, value, kind), unsafe_allow_html=True)
    st.write("")

    if outcome.diff:
        d = outcome.diff
        st.markdown(theme.badge(f"{len(d['new'])} new", "review") + "  "
                    + theme.badge(f"{len(d['resolved'])} resolved", "pass") + "  "
                    + theme.badge(f"{len(d['persisting'])} persisting", "neutral"),
                    unsafe_allow_html=True)
    if outcome.persist_error == "no_database":
        st.info("Not saved to history (no DATABASE_URL configured).")

    report = outcome.out_dir / "report.html"
    if report.exists():
        st.download_button("Download report.html", report.read_bytes(),
                           file_name="report.html", mime="text/html")

    st.write("")
    if r.findings:
        _render_findings(r.findings, outcome.diff)
    else:
        st.success("No issues found.")

    if st.button("← New scan"):
        st.session_state["scan"] = None
        st.rerun()


def screen_new_scan() -> None:
    theme.section_header("New scan", "Paste a URL, choose checks, and run.")
    scan = st.session_state.get("scan")
    if scan and scan["status"] == "running":
        _render_scan_progress(scan)
        return
    if scan and scan["status"] in ("done", "error"):
        _render_scan_results(scan)
        return

    url = st.text_input("URL to scan", placeholder="https://example.com",
                        label_visibility="collapsed")
    st.markdown('<div class="qs-hint">Checks</div>', unsafe_allow_html=True)
    selected: set[str] = set()
    cols = st.columns(2)
    for i, (key, (label, desc, default_on, heavy)) in enumerate(CHECK_INFO.items()):
        with cols[i % 2]:
            with st.container(border=True):
                checked = st.checkbox(label, value=default_on, key=f"chk_{key}")
                tag = (theme.badge("slower", "review") if heavy
                       else theme.badge("no setup", "neutral"))
                st.markdown(f'<div class="qs-hint">{desc}</div>{tag}', unsafe_allow_html=True)
                if checked:
                    selected.add(key)

    with st.expander("Advanced limits"):
        mp = st.number_input("Max pages", 1, 1000, DEFAULT_MAX_PAGES)
        md = st.number_input("Max depth", 0, 20, DEFAULT_MAX_DEPTH)
        tb = st.number_input("Time budget (seconds)", 10, 3600, int(DEFAULT_TIME_BUDGET_SECONDS))

    with st.expander("Functional testing — optional, needs a saved suite"):
        _functional_quick_run()

    can_run = bool(url.strip()) and bool(selected)
    if st.button("Run scan", type="primary", disabled=not can_run):
        limits = RunLimits(max_pages=int(mp), max_depth=int(md), time_budget_seconds=float(tb))
        _start_scan(url.strip(), selected, limits)
        st.rerun()
    if not can_run:
        st.markdown('<div class="qs-hint">Enter a URL and tick at least one check to run.</div>',
                    unsafe_allow_html=True)


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
            state["outcome"] = service.run_functional(Suite.from_file(path))
            state["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            log.exception("functional run failed")
            state["error"] = f"{type(exc).__name__}: {exc}"
            state["status"] = "error"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    state["thread"] = t
    st.session_state["frun"] = state


def _functional_quick_run() -> None:
    suites = _saved_suites()
    if not suites:
        st.markdown('<div class="qs-hint">No saved suites yet — create one in Functional.</div>',
                    unsafe_allow_html=True)
        return
    choice = st.selectbox("Saved suite", suites, format_func=lambda p: p.name, key="quick_suite")
    if st.button("Run suite", key="quick_run"):
        _run_suite_bg(choice)
        st.rerun()


def _render_functional_run(state: dict) -> None:
    if state["status"] == "running":
        with st.status(f"Running {Path(state['path']).name}…", state="running"):
            st.markdown(f"{time.time()-state['started']:.0f}s elapsed")
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
            st.markdown(theme.badge(f"status: {r.status}", r.status) + "  "
                        + theme.badge(f"{r.llm_calls} LLM calls", "neutral"),
                        unsafe_allow_html=True)
            for case in r.cases:
                st.markdown(f"###### {case.name}  " + theme.badge(case.status, case.status),
                            unsafe_allow_html=True)
                for srow in case.steps:
                    st.markdown(theme.step_row({"action": srow.action, "value": srow.message},
                                               srow.index), unsafe_allow_html=True)
    if st.button("← Back"):
        st.session_state["frun"] = None
        st.rerun()


def screen_functional() -> None:
    theme.section_header("Functional testing", "Targets, login capture, suites, and generation.")
    frun = st.session_state.get("frun")
    if frun:
        _render_functional_run(frun)
        return
    t1, t2, t3 = st.tabs(["Targets & auth", "Suites", "Generate from instruction"])
    with t1:
        _tab_targets()
    with t2:
        _tab_suites()
    with t3:
        _tab_generate()


def _tab_targets() -> None:
    st.markdown("###### Targets")
    targets = service.list_targets()
    if not targets:
        st.markdown('<div class="qs-hint">No targets yet. Add one below.</div>',
                    unsafe_allow_html=True)
    for t in targets:
        auth_badge = (theme.badge("Session active", "pass") if t.auth_kind == "storage_state"
                      else theme.badge("No auth", "neutral"))
        st.markdown(
            f'<div class="qs-card" style="margin-bottom:8px">'
            f'<div style="font-family:var(--font-display);font-weight:600">'
            f'{theme_escape(t.label or t.base_url)}</div>'
            f'<div style="margin-top:6px;display:flex;gap:8px;align-items:center">'
            f'{theme.mono(t.base_url)}{auth_badge}</div></div>', unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("**Add target**")
        base = st.text_input("Base URL", placeholder="https://app.example.com")
        label = st.text_input("Label", placeholder="My app (staging)")
        if st.button("Add target", type="primary") and base.strip():
            service.add_target(base.strip(), label.strip() or None)
            st.rerun()

    st.divider()
    st.markdown("###### One-time login capture")
    with st.container(border=True):
        st.markdown(
            "Logging in needs a real browser window so you can complete MFA/SSO yourself. "
            "This happens once per app — follow these steps:")
        cap_url = st.text_input("1 · Login URL", placeholder="https://app.example.com/login")
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        out_path = AUTH_DIR / "session.json"
        st.markdown("2 · Run this in the terminal where Streamlit is running "
                    "(it has a copy button):")
        st.code(f"qascan auth capture {cap_url or '<URL>'} --out {out_path}", language="bash")
        st.markdown("3 · Log in when the window opens · 4 · come back here.")
        if out_path.exists():
            st.markdown(theme.badge("Session active", "pass")
                        + f"  saved to {theme.mono(str(out_path))}", unsafe_allow_html=True)


def _tab_suites() -> None:
    suites = _saved_suites()
    if not suites:
        st.markdown('<div class="qs-hint">No suites yet — generate one in the next tab.</div>',
                    unsafe_allow_html=True)
        return
    path = st.selectbox("Suite", suites, format_func=lambda p: p.name)
    suite = Suite.from_file(path)
    st.markdown(
        f'<div class="qs-card"><div style="font-family:var(--font-display);font-weight:600;'
        f'font-size:16px">{theme_escape(suite.name)}</div>'
        f'<div style="margin-top:6px;display:flex;gap:8px;align-items:center">'
        f'{theme.mono(suite.target.base_url)}'
        f'{theme.badge(f"{len(suite.cases)} case(s)", "neutral")}</div></div>',
        unsafe_allow_html=True)
    st.write("")
    for case in suite.cases:
        with st.expander(f"{case.name}  ·  {case.source}", expanded=True):
            rows = "".join(theme.step_row(s.model_dump(by_alias=True, exclude_none=True), i + 1)
                           for i, s in enumerate(case.steps))
            st.markdown(f'<div class="qs-card">{rows}</div>', unsafe_allow_html=True)
            with st.popover("View raw"):
                st.json([s.model_dump(by_alias=True, exclude_none=True) for s in case.steps])
    if st.button("Run this suite", type="primary"):
        _run_suite_bg(path)
        st.rerun()


_EXAMPLES = ["Log in and reach the inventory", "Submit the contact form",
             "Add an item to the cart"]


def _tab_generate() -> None:
    url = st.text_input("URL to explore", key="gen_url", placeholder="https://www.saucedemo.com")
    st.markdown('<div class="qs-hint">Try an example:</div>', unsafe_allow_html=True)
    ex_cols = st.columns(len(_EXAMPLES))
    for col, ex in zip(ex_cols, _EXAMPLES, strict=True):
        if col.button(ex, key=f"ex_{ex}"):
            st.session_state["gen_instruction"] = ex
    instruction = st.text_area(
        "Describe what to test", key="gen_instruction",
        placeholder="log in as standard_user / secret_sauce and confirm the inventory list appears")
    st.markdown('<div class="qs-hint">Generates editable steps you review and save once — '
                'running never re-plans.</div>', unsafe_allow_html=True)

    if st.button("Draft steps", type="primary", disabled=not (url.strip() and instruction.strip())):
        with st.spinner("Exploring the page once and drafting…"):
            SUITES_DIR.mkdir(exist_ok=True)
            suite, _ = service.generate_suite(url.strip(), instruction.strip(),
                                              SUITES_DIR / "_draft.yaml")
        st.session_state["draft"] = suite.model_dump(by_alias=True, exclude_none=True)
        st.rerun()

    draft = st.session_state.get("draft")
    if not draft:
        return
    case = draft["cases"][0]
    st.markdown("###### Review & edit the drafted steps")
    edited = st.data_editor(pd.DataFrame(case["steps"]), num_rows="dynamic",
                            use_container_width=True, key="draft_editor")
    name = st.text_input("Save as", value=f"{draft['name']}.yaml")
    if st.button("Save as test case", type="primary"):
        steps = [{k: v for k, v in row.items() if pd.notna(v) and v != ""}
                 for row in edited.to_dict("records")]
        saved = Suite(name=draft["name"], target=TargetConfig(base_url=draft["target"]["base_url"]),
                      cases=[TestCase(id=case.get("id", draft["name"]), name=case["name"],
                                      source="generated",
                                      steps=[Step.model_validate(s) for s in steps])])
        (SUITES_DIR / name).write_text(saved.to_yaml(), encoding="utf-8")
        st.session_state["draft"] = None
        st.success(f"Saved {name}. Run it from the Suites tab — zero planning calls.")


# --------------------------------------------------------------------------- #
# History & review
# --------------------------------------------------------------------------- #
def screen_history() -> None:
    theme.section_header("History & review", "Past runs, findings, healed selectors, schedules.")
    sub = st.radio("View", ["Runs", "Healed selectors", "Schedules"],
                   horizontal=True, label_visibility="collapsed")
    if sub == "Runs":
        _history_runs()
    elif sub == "Healed selectors":
        _history_healed()
    else:
        _history_schedules()


def _history_runs() -> None:
    suites = service.list_suites()
    if not suites:
        st.info("No runs yet. Run your first scan from **New scan**.")
        return
    suite = st.selectbox("Suite", suites, format_func=lambda x: f"{x.name} ({x.kind})")
    runs = service.list_runs(suite.id)
    if not runs:
        st.info("No runs for this suite yet.")
        return
    chosen = st.selectbox("Run", runs,
                          format_func=lambda r: f"#{r.id} · {r.status} · "
                                                f"{r.started_at:%Y-%m-%d %H:%M}")
    try:
        detail = service.get_run_detail(chosen.id)
    except service.RunNotFound:
        st.warning("Couldn't load this run — it may have been deleted. Try another.")
        return
    except Exception:  # noqa: BLE001
        log.exception("failed to load run %s", chosen.id)
        st.warning("Couldn't load this run. Try another, or re-run the scan.")
        return

    run = detail.run
    cols = st.columns(4)
    metrics = [("Status", run.status, run.status), ("Issues", str(len(detail.findings)), None),
               ("LLM calls", str(run.llm_calls), None), ("Duration", f"{run.duration or 0}s", None)]
    for col, (label, value, kind) in zip(cols, metrics, strict=True):
        col.markdown(theme.metric_card(label, value, kind), unsafe_allow_html=True)
    st.write("")
    d = detail.diff
    st.markdown(theme.badge(f"{len(d['new'])} new", "review") + "  "
                + theme.badge(f"{len(d['resolved'])} resolved", "pass") + "  "
                + theme.badge(f"{len(d['persisting'])} persisting", "neutral"),
                unsafe_allow_html=True)
    st.write("")
    if detail.findings:
        _render_findings(detail.findings, detail.diff)
    if detail.step_results:
        st.markdown("##### Steps")
        for x in detail.step_results:
            st.markdown(theme.step_row({"action": x.status, "value": x.message}, x.step_index),
                        unsafe_allow_html=True)
    if not detail.findings and not detail.step_results:
        st.success("Clean run — nothing flagged.")


def _history_healed() -> None:
    st.markdown('<div class="qs-hint">Selectors the LLM fixed. Approve to trust them, '
                'reject to force a fresh heal next run.</div>', unsafe_allow_html=True)
    pending = service.list_pending_heals()
    if not pending:
        st.success("Nothing to review.")
        return
    for row in pending:
        with st.container(border=True):
            st.markdown(f"**{theme_escape(row.suite_name)}** · step {theme.mono(row.step_key)}",
                        unsafe_allow_html=True)
            st.markdown(theme.heal_diff("", row.selector), unsafe_allow_html=True)
            a, r = st.columns(2)
            if a.button("Approve", key=f"a{row.id}", type="primary"):
                service.approve_heal(row.id)
                st.rerun()
            if r.button("Reject", key=f"r{row.id}"):
                service.reject_heal(row.id)
                st.rerun()


def _history_schedules() -> None:
    suites = service.list_suites()
    with st.container(border=True):
        st.markdown("**Add schedule**")
        suite = st.selectbox("Suite", suites, format_func=lambda x: f"{x.name} ({x.kind})") \
            if suites else None
        cron = st.text_input("Cron (5-field)", value="*/30 * * * *")
        if st.button("Add schedule", type="primary") and suite:
            service.add_schedule(suite.id, cron)
            st.rerun()
    schedules = service.list_schedules()
    if not schedules:
        st.markdown('<div class="qs-hint">No schedules yet.</div>', unsafe_allow_html=True)
    for sched in schedules:
        with st.container(border=True):
            state_badge = (theme.badge("enabled", "pass") if sched.enabled
                           else theme.badge("disabled", "neutral"))
            st.markdown(f"**{theme_escape(sched.suite_name)}** {theme.mono(sched.cron_expr)} "
                        f"{state_badge}  ·  last {sched.last_run_at or '—'}",
                        unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            if c1.button("Toggle", key=f"t{sched.id}"):
                service.toggle_schedule(sched.id)
                st.rerun()
            if c2.button("Run now", key=f"rn{sched.id}"):
                from qascan import scheduler
                with st.spinner("Running…"):
                    st.session_state["sched_result"] = scheduler.execute_suite(sched.suite_id)
                st.rerun()
            if c3.button("Delete", key=f"del{sched.id}"):
                service.delete_schedule(sched.id)
                st.rerun()
    if st.session_state.get("sched_result"):
        st.info(str(st.session_state.pop("sched_result")))


def theme_escape(text: str) -> str:
    import html as _html
    return _html.escape(str(text))


# --------------------------------------------------------------------------- #
# App shell
# --------------------------------------------------------------------------- #
def main() -> None:
    theme.inject()
    theme.a11y_fixups()
    with st.sidebar:
        st.markdown(
            '<div style="font-family:var(--font-display);font-size:22px;font-weight:700;'
            'letter-spacing:-0.02em">◆ qascan</div>'
            '<div style="color:var(--text-muted);font-size:13px;margin-bottom:8px">'
            'Web QA scanner + functional agent</div>', unsafe_allow_html=True)

    pages = [
        st.Page(screen_new_scan, title="New scan", icon=":material/search:", default=True),
        st.Page(screen_functional, title="Functional", icon=":material/science:"),
        st.Page(screen_history, title="History", icon=":material/history:"),
    ]
    nav = st.navigation(pages, position="sidebar")

    with st.sidebar:
        st.divider()
        st.markdown('<div class="qs-hint" style="margin-bottom:6px">Severity key</div>'
                    + " ".join(theme.badge(lbl, k) for lbl, k in
                               [("pass", "pass"), ("fail", "fail"),
                                ("needs review", "review"), ("info", "info")]),
                    unsafe_allow_html=True)

    try:
        nav.run()
    except Exception as exc:  # noqa: BLE001 — clean error state, never a raw traceback
        log.exception("page render failed")
        st.error("Something went wrong rendering this page. Try reloading, or pick another "
                 f"screen.\n\n```\n{exc}\n```")


main()
