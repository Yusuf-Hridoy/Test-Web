# qascan

A web QA scanner **+** functional testing agent.

Paste a URL and pick what to check:

- **Oracle-free checks** (URL only): broken pages/links/images, console/page errors,
  and accessibility (axe-core / WCAG). Deterministic, zero LLM.
- **Functional checks** (test cases or a plain-English goal): does the flow actually
  work — with deterministic execution and LLM self-healing as a *fallback*.

Built deterministic-first: the LLM is only ever a fallback (relocating a broken
selector, judging a fuzzy assertion). **A clean run makes zero LLM calls.**

## What's inside

| Phase | Capability |
|------:|------------|
| 1 | Bounded BFS crawler + exploratory & accessibility checks → JSON + shareable HTML report |
| 2 | Functional engine: step runner, self-healing locators, assertions, auth, compile-once generator |
| 3 | PostgreSQL persistence + Streamlit dashboard (history, diffs, heal review) |
| 4 | Scheduler (APScheduler) + alerting (Slack / webhook / email) |
| 5 | Hardening: false-positive fixes, heal accuracy benchmark, robustness, docs |

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,dashboard]"
playwright install chromium
cp .env.example .env          # then fill in values (see below)
```

Requirements: Python 3.12+, and PostgreSQL 16 for Phases 3+ (a `docker-compose.yml`
is included: `docker compose up -d`).

### Configuration (`.env`)

```
GEMINI_API_KEY=...                 # only needed for self-healing / verify_nl / generate
QASCAN_MAX_PAGES=50                # crawl bounds
QASCAN_MAX_DEPTH=3
QASCAN_TIME_BUDGET_SECONDS=300
QASCAN_HEAL_THRESHOLD=0.7          # min confidence to accept an LLM heal
DATABASE_URL=postgresql+psycopg://user@localhost:5432/qascan
# Optional: QASCAN_DASHBOARD_URL, SMTP_* (email alerts), QASCAN_LOG_LEVEL=INFO
```

Create the schema:

```bash
alembic upgrade head
```

## Usage

### Scan a site (Phase 1 + 6 check types)
```bash
qascan scan https://www.saucedemo.com --max-pages 30
# writes outputs/<domain>-<ts>/report.html + results.json, and persists to the DB
open outputs/*/report.html
```

Check types (the "picker"): `exploratory`, `accessibility`, `seo` run by default
(all cheap, no extra deps). `performance` is heavy and opt-in:
```bash
qascan scan https://example.com --performance          # adds Lighthouse
qascan scan https://example.com --checks seo,performance   # pick exactly what runs
```
- **SEO** — title/meta/h1/heading-order/canonical + duplicate titles & robots/sitemap.
  Missing-alt is left to accessibility (no double-report).
- **Performance** — Lighthouse score + LCP/CLS/TBT vs Core-Web-Vitals thresholds.
  Requires **Node + `lighthouse`** (`npm i -g lighthouse`); skips with an info note if absent.

### Run a functional suite (Phase 2)
```bash
qascan run examples/saucedemo-heal.yaml
```
A suite is YAML — base URL, optional auth, and cases of steps
(`goto/click/fill/select/wait/expect/verify_nl`). Each interactive step should carry
a `hint` (natural-language description) so it can self-heal if the selector breaks.

### Generate a test from plain English (compile-once)
```bash
qascan generate https://www.saucedemo.com \
  "log in as standard_user / secret_sauce and confirm the inventory list" \
  --out my-case.yaml
# review/edit the YAML, then `qascan run my-case.yaml` — running never re-plans
```

### Capture an authenticated session
```bash
qascan auth capture https://app.example.com --out state.json
# a headed browser opens; log in manually (MFA/SSO included), press Enter
```
Reference it from a suite's `target.auth` (`kind: storage_state`).

### Web UI — do everything from the screen (Phase 6.5)
```bash
streamlit run dashboard/app.py
```
The full front door — no terminal needed:
- **New scan** — paste a URL, tick checks, Run, watch **live progress** (the scan runs
  in a background thread so the UI never freezes), Cancel, then see results inline + a
  downloadable `report.html`.
- **Functional** — manage targets, guided auth capture, view/run saved suites, and the
  generate-from-instruction flow (drafts editable steps → save a frozen suite).
- **History** — run history, drill-down with diffs, the healed-selector review queue,
  and schedules (toggle / run now).

Both the CLI and the UI call the same **service layer** (`qascan/service.py`) — no
business logic lives in either shell, so they produce identical results on the same input.

### Schedule + alerts (Phase 4)
```bash
qascan suites                      # list suite ids
qascan schedule add 1 "*/30 * * * *"
qascan notify-add 1 slack "https://hooks.slack.com/services/..."
qascan worker                      # long-running; triggers schedules, alerts on regressions
qascan trigger 1                   # run a suite now (manual)
```
Alerts fire only on a **new** critical/regression or a failing case — never on
unchanged known issues. Failed functional cases retry once; pass-on-retry is marked
*flaky*, not a failure.

## Architecture rules (non-negotiable)

- **Deterministic-first, LLM-fallback.** Playwright/axe/HTTP status run first; the LLM
  is a fallback only. A clean run = zero LLM calls.
- **Single LLM touchpoint** — all Gemini access goes through `qascan/llm.py`.
- **Grounding** — LLM output is traceable to input or marked `[VERIFY]`; never invented.
- **Determinism** — temperature 0, every LLM result cached by a stable key.
- **Everything bounded** — crawls, retries, timeouts, one heal per step.

## Quality artifacts

- `docs/eval-results.md` — self-healing accuracy benchmark (success / **false-heal** rates).
  Regenerate: `python scripts/heal_eval.py`.
- `docs/validation-log.md` — real-world validation over 8 public sites with the
  false-positive fixes. Reproduce: `python scripts/validate.py`.

## Development

```bash
ruff check .          # lint
ruff format .         # format
pytest -q             # tests (DB tests need DATABASE_URL_TEST; they skip otherwise)
```

Tests are network-free: a local fixture HTTP server + a fake LLM cover the engine
paths deterministically (good for CI). LLM-dependent behavior is unit-tested against
a patched touchpoint; live behavior is exercised by `scripts/heal_eval.py`.

Layout:
```
qascan/
  crawler.py            # bounded BFS
  urls.py               # URL normalize / domain / non-HTML helpers
  checks/               # exploratory + accessibility
  functional/           # schema, locators (self-heal), executor, assertions, auth, generator
  service.py            # service layer — the shared entry point for CLI + UI
  llm.py                # single Gemini touchpoint
  report.py             # JSON + HTML reports
  db/                   # SQLAlchemy models, session, repository
  scheduler.py          # APScheduler worker + scheduled-run execution
  notify.py             # Slack / webhook / email alerting
dashboard/app.py        # Streamlit
migrations/             # Alembic
scripts/                # heal_eval.py, validate.py
```

## Docker (runner)

A `Dockerfile` based on the official Playwright Python image (browsers preinstalled)
is provided for reproducible deploys:

```bash
docker build -t qascan .
docker run --rm --env-file .env qascan qascan scan https://example.com --no-db
```
