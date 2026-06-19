# CLAUDE.md — qascan

> Read this fully before doing anything. It defines the rules that apply to **every** phase.
> Implement **one phase at a time**, in order, from `docs/phase-N.md`. Do not start a phase until the previous phase's acceptance criteria pass. Do not pull work forward from later phases.

---

## What this is

A web QA scanner + agent. The user pastes a URL and picks what to check:

- **Oracle-free checks** (need only a URL — the standard is universal): broken pages, accessibility, performance, SEO.
- **Functional checks** (need test cases or a plain-English goal — correctness is app-specific): "does checkout actually work."

**Launch scope is two oracle-free checks: exploratory crawl + accessibility.** Everything else is roadmap. Build only what the current phase asks for.

---

## The non-negotiable architectural rules

These apply everywhere. Violating them is a bug even if tests pass.

1. **Deterministic-first, LLM-fallback.** Never call the LLM on a step that a deterministic method can handle. Playwright locators, axe-core, HTTP status codes, Lighthouse — these are deterministic and run first. The LLM is a *fallback* (relocating a broken selector, interpreting a fuzzy assertion), never the primary path. A run with no failures should make **zero** LLM calls.

2. **Single LLM touchpoint.** All LLM access goes through `qascan/llm.py`. No other module imports the Gemini SDK or builds prompts. One function in, structured data out. This makes the LLM swappable and the cost auditable.

3. **Grounding — the LLM may not invent facts.** Every LLM output must be traceable to input we gave it. When the LLM produces a value, tag its provenance:
   - `DERIVED` — logically follows from given input.
   - `COMPUTED` — calculated from given numbers.
   - `[VERIFY]` — the model is unsure; surface to the user, never present as fact.
   Never let the LLM emit a specific number, selector, or assertion result that wasn't grounded in input. If it can't ground it, it returns `[VERIFY]`, not a guess.

4. **Determinism of output.** Same input → same report. Pin Gemini `temperature=0`. Cache every LLM result by a stable key. A QA tool that gives different answers on identical input is worthless.

5. **Everything is bounded.** Crawls, retries, LLM calls — all have hard limits (max pages, max depth, time budget, one retry per step). Nothing runs unbounded.

---

## Tech stack (use these, these versions or newer)

- **Python 3.12+**
- **Playwright (Python)** — `playwright`, async API (`playwright.async_api`). Chromium only for now.
- **Gemini** — model string `gemini-2.5-flash-lite`. SDK: `google-genai`. Temperature 0. JSON-only responses.
- **axe-core** — vendored JS, injected into the page (see `docs/phase-1.md`).
- **Lighthouse** — deferred to Phase 6.
- **CLI** — `typer`.
- **HTTP (link checks)** — `httpx` (async).
- **Config/validation** — `pydantic` v2 for all data models (findings, test cases, configs).
- **DB (Phase 3+)** — PostgreSQL via `sqlalchemy` 2.x + `alembic` migrations.
- **API (Phase 5+)** — FastAPI.
- **Dashboard** — Streamlit for v1, Next.js later. Do not host the Playwright runner on Streamlit Community Cloud.
- **Tests** — `pytest`, `pytest-asyncio`.
- **Lint/format** — `ruff` (lint + format). Type hints everywhere; check with `mypy` if configured.

---

## Repo structure (target)

```
qascan/
├── CLAUDE.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── qascan/
│   ├── __init__.py
│   ├── cli.py              # typer entry point
│   ├── config.py          # settings, env loading, run limits
│   ├── findings.py        # Finding model + severity enum
│   ├── llm.py             # SINGLE LLM touchpoint (Gemini)
│   ├── crawler.py         # BFS crawler with hard limits   [P1]
│   ├── checks/
│   │   ├── __init__.py
│   │   ├── exploratory.py # broken links/images, console/page errors  [P1]
│   │   └── accessibility.py # axe-core injection + mapping  [P1]
│   ├── report.py          # JSON + HTML report writer   [P1]
│   └── functional/        # [P2+]
│       ├── schema.py      # TestCase / Step models
│       ├── executor.py    # deterministic step runner
│       ├── locators.py    # locate + self-heal
│       ├── assertions.py  # deterministic + verify_nl
│       ├── generator.py   # plain instruction -> steps (compile-once)
│       └── auth.py        # storage-state injection
├── vendor/
│   └── axe.min.js
├── tests/
├── outputs/               # generated reports (gitignored)
└── docs/
    ├── phase-1.md ... phase-6.md
```

---

## Conventions

- **Async throughout** the Playwright/IO paths. CLI command bodies call `asyncio.run(...)`.
- **Pydantic models** for every structured object that crosses a module boundary. No bare dicts as public interfaces.
- **No secrets in code or logs.** Read from env via `config.py`. `.env` is gitignored; keep `.env.example` current.
- **No `localStorage`/`sessionStorage` in any web UI** (Streamlit/Next) — use server/session state.
- **Errors are findings, not crashes.** A broken page is a *result* to report, not an exception that aborts the scan. Catch per-page/per-step, record, continue.
- **Every public function gets a docstring and type hints.**
- **Output goes to `outputs/`**, never committed.

## Environment variables (`.env.example`)

```
GEMINI_API_KEY=
QASCAN_MAX_PAGES=50
QASCAN_MAX_DEPTH=3
QASCAN_TIME_BUDGET_SECONDS=300
# DB / API / billing vars added in their phases
```

## Commands (keep these working)

```
# install
pip install -e ".[dev]" && playwright install chromium
# run a scan (Phase 1)
qascan scan https://www.saucedemo.com --max-pages 30
# tests
pytest -q
# lint + format
ruff check . && ruff format .
```

---

## Safety rules (hard stops)

- **Never automate around MFA or CAPTCHA.** Auth is handled by human-captured storage-state (Phase 2/6), never by solving challenges.
- **Staging-only by default.** Don't build anything that encourages pointing the functional agent at production.
- **Respect targets.** Same-domain crawl only, polite delay, honor robots.txt for the crawler, sane concurrency.
- **No credentials stored in plaintext.** When credential storage arrives (Phase 5), it's encrypted.

---

## Definition of done (every phase)

1. The phase's acceptance criteria in `docs/phase-N.md` all pass, demonstrably (show the command + output).
2. `ruff check .` and `pytest -q` are green.
3. No rule in this file is violated.
4. `.env.example` and this file are updated if the phase introduced new config or structure.
5. A one-paragraph summary of what changed and how to run it.

When a phase is done, stop and report. Do not begin the next phase without being told.