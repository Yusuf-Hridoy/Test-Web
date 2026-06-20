"""Typer CLI entry point. scan (P1) + run / generate / auth (P2)."""

from __future__ import annotations

from pathlib import Path

import typer

from . import service
from .config import RunLimits

app = typer.Typer(add_completion=False, help="qascan — oracle-free web QA scanner")
auth_app = typer.Typer(add_completion=False, help="Authentication helpers.")
app.add_typer(auth_app, name="auth")


@app.callback()
def _main() -> None:
    """qascan — oracle-free web QA scanner."""
    # Presence of a callback keeps `scan` as a named subcommand (per CLAUDE.md).
    import logging
    import os

    logging.basicConfig(
        level=os.getenv("QASCAN_LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@app.command()
def scan(
    url: str = typer.Argument(..., help="Seed URL to scan."),
    max_pages: int | None = typer.Option(None, "--max-pages", help="Max pages to crawl."),
    max_depth: int | None = typer.Option(None, "--max-depth", help="Max crawl depth."),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Time budget in seconds."
    ),
    out: Path = typer.Option(Path("outputs"), "--out", help="Output root directory."),
    no_db: bool = typer.Option(False, "--no-db", help="Skip persisting to the database."),
    checks: str | None = typer.Option(
        None, "--checks",
        help="Comma list: exploratory,accessibility,seo,performance "
             "(default: exploratory,accessibility,seo).",
    ),
    performance: bool = typer.Option(
        False, "--performance", help="Add the heavy Lighthouse performance check."
    ),
) -> None:
    """Crawl URL within hard limits and write a health report."""
    from .crawler import DEFAULT_CHECKS

    base = service.default_limits()
    limits = RunLimits(
        max_pages=max_pages if max_pages is not None else base.max_pages,
        max_depth=max_depth if max_depth is not None else base.max_depth,
        time_budget_seconds=timeout if timeout is not None else base.time_budget_seconds,
    )
    selected = (
        {c.strip() for c in checks.split(",") if c.strip()} if checks else set(DEFAULT_CHECKS)
    )
    if performance:
        selected.add("performance")

    typer.echo(f"Scanning {url} (max_pages={limits.max_pages}, "
               f"max_depth={limits.max_depth}, budget={limits.time_budget_seconds}s) "
               f"checks={','.join(sorted(selected))}…")

    # Same service-layer call the UI makes.
    outcome = service.run_scan(url, checks=selected, limits=limits, out_root=out,
                               persist=not no_db)
    result = outcome.crawl
    counts = service.severity_counts(result.findings)

    typer.echo(
        f"Done: {result.pages_scanned} page(s), "
        f"{len(result.findings)} finding(s) "
        f"(critical={counts.get('critical', 0)}, warning={counts.get('warning', 0)}, "
        f"minor={counts.get('minor', 0)}) · stopped={result.stopped_reason} · "
        f"{result.duration_seconds}s"
    )
    typer.echo(f"Report: {outcome.out_dir / 'report.html'}")
    _echo_persist(outcome.run_id, outcome.persist_error, outcome.diff)
    # Phase 1 is an audit, not a gate — always exit 0.


def _echo_persist(run_id, persist_error, diff=None) -> None:
    if run_id is not None:
        msg = f"Saved run #{run_id} to database"
        if diff is not None:
            msg += (f" · diff vs previous: {len(diff['new'])} new, "
                    f"{len(diff['resolved'])} resolved, {len(diff['persisting'])} persisting")
        typer.echo(msg + ".")
    elif persist_error == "no_database":
        typer.echo("No DATABASE_URL set — skipped persistence (results still on disk).")
    elif persist_error:
        typer.echo(f"Warning: could not persist to database ({persist_error}).")


@app.command()
def run(
    suite: Path = typer.Argument(..., help="Path to a Suite YAML/JSON file."),
    out: Path = typer.Option(Path("outputs"), "--out", help="Output root directory."),
    continue_on_fail: bool = typer.Option(
        False, "--continue-on-fail", help="Keep running a case after a step fails."
    ),
    threshold: float = typer.Option(
        0.7, "--heal-threshold", help="Min confidence to accept an LLM heal."
    ),
    no_db: bool = typer.Option(False, "--no-db", help="Skip persisting to the database."),
) -> None:
    """Run a functional suite end-to-end and write a report."""
    from .functional.schema import Suite

    loaded = Suite.from_file(suite)
    typer.echo(f"Running suite '{loaded.name}' ({len(loaded.cases)} case(s)) "
               f"against {loaded.target.base_url}…")
    outcome = service.run_functional(loaded, out_root=out, persist=not no_db,
                                     continue_on_fail=continue_on_fail, threshold=threshold)
    result = outcome.result

    if result.status == "session_expired":
        typer.echo(f"Session expired — suite NOT run. {result.message}")
    else:
        passed = sum(1 for c in result.cases if c.status == "pass")
        typer.echo(
            f"Done: {passed}/{len(result.cases)} case(s) passed · status={result.status} · "
            f"{result.llm_calls} LLM call(s) · {len(result.healed_for_review)} healed "
            f"(needs review) · {result.duration_seconds}s"
        )
    typer.echo(f"Report: {outcome.out_dir / 'report.html'}")
    _echo_persist(outcome.run_id, outcome.persist_error)


@app.command()
def generate(
    url: str = typer.Argument(..., help="URL to explore once."),
    instruction: str = typer.Argument(..., help="Plain-English flow to test."),
    out: Path = typer.Option(..., "--out", help="Where to write the generated Suite YAML."),
) -> None:
    """Draft an editable TestCase YAML from a plain instruction (compile-once)."""
    typer.echo(f"Exploring {url} once and drafting a test case…")
    suite, path = service.generate_suite(url, instruction, out)
    n = len(suite.cases[0].steps) if suite.cases else 0
    typer.echo(f"Wrote generated suite to {path} ({n} step(s)). "
               "Review/edit it, then run with `qascan run`.")


@auth_app.command("capture")
def auth_capture(
    url: str = typer.Argument(..., help="URL to open for manual login."),
    out: Path = typer.Option(..., "--out", help="Where to save the storage-state JSON."),
) -> None:
    """Open a headed browser, let a human log in, then save the session state."""
    path = service.capture_auth(url, out)
    typer.echo(f"Saved storage state to {path}. Reference it from a suite's target.auth.")


schedule_app = typer.Typer(add_completion=False, help="Manage scheduled runs.")
app.add_typer(schedule_app, name="schedule")


@app.command()
def suites() -> None:
    """List persisted suites with their ids (for scheduling/triggering)."""
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.scalars(select(models.Suite).order_by(models.Suite.id)).all()
        if not rows:
            typer.echo("No suites yet. Run a scan or functional suite first.")
            return
        for x in rows:
            typer.echo(f"  #{x.id:<3} {x.kind:<11} {x.name}")


@app.command()
def trigger(suite_id: int = typer.Argument(..., help="Suite id (see `qascan suites`).")) -> None:
    """Run a suite now (manual trigger). Honors the overlap guard."""
    from . import scheduler

    typer.echo(f"Triggering suite #{suite_id}…")
    summary = scheduler.execute_suite(suite_id)
    typer.echo(str(summary))


@app.command()
def worker() -> None:
    """Run the scheduler worker (blocks): loads enabled schedules and triggers runs."""
    from . import scheduler

    scheduler.worker()


@schedule_app.command("add")
def schedule_add(
    suite_id: int = typer.Argument(..., help="Suite id."),
    cron: str = typer.Argument(..., help='5-field cron, e.g. "*/5 * * * *".'),
) -> None:
    """Schedule a suite on a cron expression."""
    from .db import repository
    from .db.session import session_scope

    with session_scope() as s:
        sched = repository.add_schedule(s, suite_id, cron)
        s.flush()
        typer.echo(f"Scheduled suite #{suite_id} as schedule #{sched.id} ({cron}). "
                   "Start `qascan worker` to run it.")


@schedule_app.command("list")
def schedule_list() -> None:
    """List schedules."""
    from sqlalchemy import select

    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        rows = s.scalars(select(models.Schedule).order_by(models.Schedule.id)).all()
        if not rows:
            typer.echo("No schedules.")
            return
        for r in rows:
            state = "enabled" if r.enabled else "disabled"
            typer.echo(f"  #{r.id} suite={r.suite_id} '{r.cron_expr}' {state} "
                       f"last={r.last_run_at}")


@app.command("notify-add")
def notify_add(
    suite_id: int = typer.Argument(..., help="Suite id."),
    channel: str = typer.Argument(..., help="slack | email | webhook."),
    target: str = typer.Argument(..., help="Webhook/Slack URL or email recipient."),
) -> None:
    """Add a notification channel for a suite."""
    from .db import models
    from .db.session import session_scope

    with session_scope() as s:
        cfg = models.NotifyConfig(suite_id=suite_id, channel=channel, target=target)
        s.add(cfg)
        s.flush()
        typer.echo(f"Added {channel} notification #{cfg.id} for suite #{suite_id}.")


if __name__ == "__main__":
    app()
