"""qascan dashboard (Streamlit). Reads PostgreSQL directly — no API yet.

Pages: Overview (suites + trend), Run detail (findings, evidence, diff vs prior),
Healed selectors (approve/reject the review queue). Uses Streamlit session state
only — no localStorage.

Run with:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from qascan.db import models, repository
from qascan.db.session import get_sessionmaker

st.set_page_config(page_title="qascan", page_icon="🔎", layout="wide")

_SEV_ORDER = {"critical": 0, "warning": 1, "minor": 2, "info": 3}
_STATUS_EMOJI = {
    "pass": "🟢", "healthy": "🟢", "fail": "🔴", "critical": "🔴",
    "needs_review": "🟡", "warning": "🟡", "minor": "🔵", "session_expired": "🟠",
}


@st.cache_resource
def _sessionmaker():
    return get_sessionmaker()


def _session():
    return _sessionmaker()()


def page_overview() -> None:
    st.header("Overview")
    with _session() as s:
        suites = s.scalars(select(models.Suite).order_by(models.Suite.id)).all()
        if not suites:
            st.info("No runs yet. Run `qascan scan <url>` or `qascan run <suite>.yaml`.")
            return
        for suite in suites:
            runs = s.scalars(
                select(models.Run).where(models.Run.suite_id == suite.id)
                .order_by(models.Run.id)
            ).all()
            last = runs[-1] if runs else None
            emoji = _STATUS_EMOJI.get(last.status, "⚪") if last else "⚪"
            col1, col2 = st.columns([3, 2])
            with col1:
                st.subheader(f"{emoji} {suite.name}")
                st.caption(f"{suite.kind} · {len(runs)} run(s) · "
                           f"last status: {last.status if last else '—'}")
            with col2:
                if len(runs) >= 2:
                    if suite.kind == "scan":
                        series = [
                            s.scalar(select(func.count(models.Finding.id))
                                     .where(models.Finding.run_id == r.id)) or 0
                            for r in runs[-10:]
                        ]
                        st.caption("findings over last runs")
                    else:
                        series = [
                            s.scalar(select(func.count(models.StepResult.id)).where(
                                models.StepResult.run_id == r.id,
                                models.StepResult.status == "fail")) or 0
                            for r in runs[-10:]
                        ]
                        st.caption("failed steps over last runs")
                    st.line_chart(pd.DataFrame({"v": series}), height=80)


def page_run_detail() -> None:
    st.header("Run detail")
    with _session() as s:
        suites = s.scalars(select(models.Suite).order_by(models.Suite.name)).all()
        if not suites:
            st.info("No data yet.")
            return
        suite = st.selectbox("Suite", suites, format_func=lambda x: f"{x.name} ({x.kind})")
        if st.button("▶️ Run now", key=f"run_now_{suite.id}"):
            from qascan import scheduler
            with st.spinner(f"Running {suite.name}…"):
                summary = scheduler.execute_suite(suite.id)
            st.success(f"Done: {summary}")
            st.rerun()
        runs = s.scalars(
            select(models.Run).where(models.Run.suite_id == suite.id)
            .order_by(models.Run.id.desc())
        ).all()
        if not runs:
            st.info("This suite has no runs.")
            return
        run = st.selectbox(
            "Run", runs,
            format_func=lambda r: f"#{r.id} · {r.status} · {r.started_at:%Y-%m-%d %H:%M}",
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Status", run.status)
        c2.metric("Pages / Duration", f"{run.pages_scanned or '—'} / {run.duration or 0}s")
        c3.metric("LLM calls", run.llm_calls)
        c4.metric("Est. cost", f"${run.llm_cost_estimate or 0:.4f}")

        # Diff vs prior run.
        diff = repository.diff_findings(s, run)
        st.subheader("Change vs previous run")
        d1, d2, d3 = st.columns(3)
        d1.metric("🆕 New", len(diff["new"]))
        d2.metric("✅ Resolved", len(diff["resolved"]))
        d3.metric("➖ Persisting", len(diff["persisting"]))
        with st.expander("Diff detail"):
            for label in ("new", "resolved", "persisting"):
                if diff[label]:
                    st.markdown(f"**{label.title()}**")
                    st.dataframe(pd.DataFrame(diff[label]), use_container_width=True)

        # Findings.
        findings = s.scalars(select(models.Finding).where(models.Finding.run_id == run.id)).all()
        if findings:
            st.subheader(f"Findings ({len(findings)})")
            rows = sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))
            st.dataframe(
                pd.DataFrame([
                    {"severity": f.severity, "check": f.check, "type": f.type,
                     "title": f.title, "page": f.page_url, "evidence": f.evidence_path}
                    for f in rows
                ]),
                use_container_width=True,
            )

        # Step results.
        steps = s.scalars(
            select(models.StepResult).where(models.StepResult.run_id == run.id)
            .order_by(models.StepResult.id)
        ).all()
        if steps:
            st.subheader(f"Steps ({len(steps)})")
            st.dataframe(
                pd.DataFrame([
                    {"#": st_.step_index, "status": st_.status, "message": st_.message,
                     "evidence": st_.evidence_path}
                    for st_ in steps
                ]),
                use_container_width=True,
            )
            for st_ in steps:
                if st_.evidence_path:
                    try:
                        st.image(st_.evidence_path, caption=f"step {st_.step_index}", width=360)
                    except Exception:  # noqa: BLE001
                        st.caption(f"evidence: {st_.evidence_path}")


def page_healed() -> None:
    st.header("Healed selectors — review queue")
    st.caption("Selectors fixed by the LLM. Approve to trust them, or reject to "
               "force a fresh heal on the next run.")
    with _session() as s:
        pending = s.scalars(
            select(models.SelectorCache).where(models.SelectorCache.reviewed.is_(False))
            .order_by(models.SelectorCache.id)
        ).all()
        if not pending:
            st.success("Nothing to review. 🎉")
            return
        for row in pending:
            suite = s.get(models.Suite, row.suite_id)
            with st.container(border=True):
                st.markdown(f"**{suite.name if suite else row.suite_id}** · step `{row.step_key}`")
                st.code(row.selector, language="text")
                st.caption(f"healed at {row.healed_at:%Y-%m-%d %H:%M}")
                a, r = st.columns(2)
                if a.button("✅ Approve", key=f"a{row.id}"):
                    repository.approve_heal(s, row)
                    s.commit()
                    st.rerun()
                if r.button("🔁 Reject (re-heal next run)", key=f"r{row.id}"):
                    repository.reject_heal(s, row, suite.name if suite else "")
                    s.commit()
                    st.rerun()


def page_schedules() -> None:
    st.header("Schedules")
    with _session() as s:
        suites = s.scalars(select(models.Suite).order_by(models.Suite.name)).all()
        if not suites:
            st.info("No suites yet.")
            return
        with st.form("add_schedule"):
            suite = st.selectbox("Suite", suites, format_func=lambda x: f"{x.name} ({x.kind})")
            cron = st.text_input("Cron (5-field)", value="*/5 * * * *")
            if st.form_submit_button("Add schedule"):
                repository.add_schedule(s, suite.id, cron)
                s.commit()
                st.rerun()

        rows = s.scalars(select(models.Schedule).order_by(models.Schedule.id)).all()
        for r in rows:
            suite = s.get(models.Suite, r.suite_id)
            with st.container(border=True):
                st.markdown(f"**{suite.name if suite else r.suite_id}** · `{r.cron_expr}` · "
                            f"{'🟢 enabled' if r.enabled else '⚪ disabled'}")
                st.caption(f"last run: {r.last_run_at or '—'} · next: {r.next_run_at or '—'}")
                c1, c2 = st.columns(2)
                if c1.button("Toggle enabled", key=f"t{r.id}"):
                    r.enabled = not r.enabled
                    s.commit()
                    st.rerun()
                if c2.button("Delete", key=f"d{r.id}"):
                    s.delete(r)
                    s.commit()
                    st.rerun()


PAGES = {
    "Overview": page_overview,
    "Run detail": page_run_detail,
    "Schedules": page_schedules,
    "Healed selectors": page_healed,
}

st.sidebar.title("🔎 qascan")
st.sidebar.caption("🟢 pass · 🔴 fail (deterministic) · 🟡 needs review "
                   "(verify_nl / generated) · 🔵 info/unverified")
choice = st.sidebar.radio("Page", list(PAGES), key="nav")

try:
    PAGES[choice]()
except OperationalError as exc:
    st.error("Cannot reach the database. Is Postgres running and DATABASE_URL set?\n\n"
             f"```\n{exc}\n```")
except Exception as exc:  # noqa: BLE001 — show a clean error state, not a raw traceback
    st.error(f"Something went wrong loading this page:\n\n```\n{exc}\n```")
