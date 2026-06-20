"""Typer CLI entry point. scan (P1) + run / generate / auth (P2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from .config import RunLimits, Settings
from .crawler import crawl
from .report import write_report

app = typer.Typer(add_completion=False, help="qascan — oracle-free web QA scanner")
auth_app = typer.Typer(add_completion=False, help="Authentication helpers.")
app.add_typer(auth_app, name="auth")


@app.callback()
def _main() -> None:
    """qascan — oracle-free web QA scanner."""
    # Presence of a callback keeps `scan` as a named subcommand (per CLAUDE.md).


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
) -> None:
    """Crawl URL within hard limits and write a health report."""
    settings = Settings.from_env()
    base = settings.to_limits()
    limits = RunLimits(
        max_pages=max_pages if max_pages is not None else base.max_pages,
        max_depth=max_depth if max_depth is not None else base.max_depth,
        time_budget_seconds=timeout if timeout is not None else base.time_budget_seconds,
    )

    typer.echo(f"Scanning {url} (max_pages={limits.max_pages}, "
               f"max_depth={limits.max_depth}, budget={limits.time_budget_seconds}s)…")

    result = asyncio.run(crawl(url, limits))
    out_dir = write_report(url, result, out_root=out)

    counts = {}
    for f in result.findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1

    typer.echo(
        f"Done: {result.pages_scanned} page(s), "
        f"{len(result.findings)} finding(s) "
        f"(critical={counts.get('critical', 0)}, warning={counts.get('warning', 0)}, "
        f"minor={counts.get('minor', 0)}) · stopped={result.stopped_reason} · "
        f"{result.duration_seconds}s"
    )
    typer.echo(f"Report: {out_dir / 'report.html'}")
    if not no_db:
        _persist(lambda s: _report_scan_persist(s, url, result))
    # Phase 1 is an audit, not a gate — always exit 0.


def _persist(work) -> None:
    """Run a persistence callback in a transaction; degrade gracefully if no DB."""
    from .db.session import DatabaseNotConfigured, session_scope

    try:
        with session_scope() as session:
            work(session)
    except DatabaseNotConfigured:
        typer.echo("No DATABASE_URL set — skipped persistence (results still on disk).")
    except Exception as exc:  # noqa: BLE001 — persistence must never abort a run
        typer.echo(f"Warning: could not persist to database ({type(exc).__name__}: {exc}).")


def _report_scan_persist(session, url, result) -> None:
    from .db import repository

    run = repository.persist_scan(session, url, result)
    session.flush()
    diff = repository.diff_findings(session, run)
    typer.echo(
        f"Saved run #{run.id} to database · diff vs previous: "
        f"{len(diff['new'])} new, {len(diff['resolved'])} resolved, "
        f"{len(diff['persisting'])} persisting."
    )


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
    from .functional.executor import run_suite
    from .functional.schema import Suite

    loaded = Suite.from_file(suite)
    typer.echo(f"Running suite '{loaded.name}' ({len(loaded.cases)} case(s)) "
               f"against {loaded.target.base_url}…")
    result, out_dir = asyncio.run(
        run_suite(loaded, out_root=out, continue_on_fail=continue_on_fail, threshold=threshold)
    )

    if result.status == "session_expired":
        typer.echo(f"Session expired — suite NOT run. {result.message}")
    else:
        passed = sum(1 for c in result.cases if c.status == "pass")
        typer.echo(
            f"Done: {passed}/{len(result.cases)} case(s) passed · status={result.status} · "
            f"{result.llm_calls} LLM call(s) · {len(result.healed_for_review)} healed "
            f"(needs review) · {result.duration_seconds}s"
        )
    typer.echo(f"Report: {out_dir / 'report.html'}")
    if not no_db and result.status != "session_expired":
        def _save(session):
            from .db import repository
            run = repository.persist_functional(session, loaded, result)
            session.flush()
            typer.echo(f"Saved run #{run.id} to database.")
        _persist(_save)


@app.command()
def generate(
    url: str = typer.Argument(..., help="URL to explore once."),
    instruction: str = typer.Argument(..., help="Plain-English flow to test."),
    out: Path = typer.Option(..., "--out", help="Where to write the generated Suite YAML."),
) -> None:
    """Draft an editable TestCase YAML from a plain instruction (compile-once)."""
    from .functional.generator import generate as generate_case

    typer.echo(f"Exploring {url} once and drafting a test case…")
    suite, path = asyncio.run(generate_case(url, instruction, out))
    n = len(suite.cases[0].steps) if suite.cases else 0
    typer.echo(f"Wrote generated suite to {path} ({n} step(s)). "
               "Review/edit it, then run with `qascan run`.")


@auth_app.command("capture")
def auth_capture(
    url: str = typer.Argument(..., help="URL to open for manual login."),
    out: Path = typer.Option(..., "--out", help="Where to save the storage-state JSON."),
) -> None:
    """Open a headed browser, let a human log in, then save the session state."""
    from .functional.auth import capture_storage_state

    path = asyncio.run(capture_storage_state(url, out))
    typer.echo(f"Saved storage state to {path}. Reference it from a suite's target.auth.")


if __name__ == "__main__":
    app()
