# Phase 1 — Free oracle-free scanner

**Goal:** A URL-only, zero-config scanner that crawls a site within hard limits and produces a shareable health report covering two checks: **exploratory** (broken pages/links/images, console & page errors) and **accessibility** (axe-core / WCAG). No auth, no test cases, no database, no LLM required for the checks themselves.

**Why first:** This is the free funnel and a standalone portfolio piece. It uses only deterministic, proven tooling, so it ships fast and low-risk. Self-healing and the LLM engine come in Phase 2.

**Non-goals (do NOT build here):** functional testing, login/auth, persistence/DB, scheduling, multi-tenancy, performance/SEO checks, any web UI beyond a static HTML report. Resist all of it.

---

## Dependencies

Add to `pyproject.toml`: `playwright`, `httpx`, `typer`, `pydantic>=2`, `python-dotenv`; dev: `pytest`, `pytest-asyncio`, `ruff`. Then `playwright install chromium`.

---

## Build order (tasks)

### 1. Scaffold
- Create `pyproject.toml` (package `qascan`, entry point `qascan = "qascan.cli:app"`), `.gitignore` (`outputs/`, `.env`, `__pycache__`, `.venv`), `.env.example` per CLAUDE.md.
- Create package skeleton: `qascan/__init__.py`, `cli.py`, `config.py`, `findings.py`, `crawler.py`, `checks/__init__.py`, `checks/exploratory.py`, `checks/accessibility.py`, `report.py`.
- `config.py`: a pydantic `Settings` (or simple loader) reading env: `max_pages`, `max_depth`, `time_budget_seconds`, with the CLAUDE.md defaults. Plus a `RunLimits` dataclass passed into the crawler.

### 2. Finding model (`findings.py`)
Define the shared output type. All checks emit these.

```python
class Severity(str, Enum):
    CRITICAL = "critical"   # page won't load, 500s, form submit errors
    WARNING  = "warning"    # broken link/image, console errors
    MINOR    = "minor"      # accessibility nits, missing alt text
    INFO     = "info"

class Finding(BaseModel):
    id: str                 # stable hash of (type + page_url + locator/detail)
    check: str              # "exploratory" | "accessibility"
    type: str               # "broken_link" | "console_error" | "wcag_violation" ...
    severity: Severity
    title: str              # short, human ("3 broken links")
    detail: str             # specifics (the URL, the rule, the element)
    page_url: str           # where it was found
    evidence: str | None = None   # optional screenshot path
```

Stable `id` matters — it's how Phase 3+ will diff runs ("this finding is new since yesterday"). Hash deterministic inputs only.

### 3. Crawler (`crawler.py`)
Async BFS from the seed URL. **Hard-bounded.**

Algorithm:
- Queue seeded with `(url=seed, depth=0)`. `visited: set[str]`.
- Launch one Chromium context. Reuse one page or a small pool.
- Loop while queue non-empty AND `len(visited) < max_pages` AND elapsed < `time_budget_seconds`:
  - Pop `(url, depth)`; skip if visited or different registrable domain than seed; mark visited.
  - `response = await page.goto(url, wait_until="domcontentloaded", timeout=...)`.
  - Hand the loaded page to the per-page checks (task 4 & 5); collect findings.
  - If `depth < max_depth`: extract same-domain `<a href>` links, normalize (strip fragments, resolve relative, drop mailto/tel/javascript), enqueue unseen.
  - Polite small delay between navigations.
- Return `(findings, pages_scanned, stopped_reason)` where `stopped_reason ∈ {completed, max_pages, time_budget}`.

Rules:
- **Same registrable domain only** (use the seed's eTLD+1; don't wander to external sites — but *do* check external links for liveness in the link check, just don't crawl into them).
- Normalize URLs to dedupe (`https://x.com/a` == `https://x.com/a/` decision — pick one and be consistent).
- Honor `robots.txt` disallow for crawling (fetch once, parse, respect). Liveness checks of individual links are fine.
- Never throw out of the loop on a single bad page — wrap each page in try/except, emit a `critical` finding for load failures, continue.

### 4. Exploratory checks (`checks/exploratory.py`)
Run against each loaded page. Each returns `list[Finding]`.

- **Page load status:** non-2xx/3xx response or navigation error → `critical` `page_error`.
- **Console errors:** before `goto`, attach `page.on("console", ...)` capturing `msg.type == "error"`; attach `page.on("pageerror", ...)` for uncaught JS exceptions → `warning` `console_error`. (Collect per page, then drain.)
- **Broken images:** in-page evaluate — for each `img`, check `naturalWidth === 0 && complete` OR fetch its `src` status; failures → `warning` `broken_image`.
- **Broken links:** collect all same-and-other-domain `<a href>` absolute URLs on the page; dedupe globally; check liveness with `httpx` (HEAD, fall back to GET on 405) with a concurrency cap and timeout; 4xx/5xx/timeout → `warning` `broken_link`. Cache checked URLs across pages so each external URL is hit once.
- **Dead buttons (optional, keep simple):** anchors with empty/`#`/`javascript:void` href that look like nav → `minor` `dead_link`. Skip if noisy.

### 5. Accessibility check (`checks/accessibility.py`)
- Vendor axe-core: download `axe.min.js` into `vendor/` (document the version in a comment). Do not fetch from CDN at runtime — bundle it.
- For each loaded page: `await page.add_script_tag(path="vendor/axe.min.js")`, then `results = await page.evaluate("async () => await axe.run()")`.
- Map each `violation` to a `Finding`: `type="wcag_violation"`, `title=violation.help`, `detail=` rule id + impact + first node target + help URL, `severity` from axe `impact` (`critical/serious → warning`, `moderate → minor`, `minor → minor`). (Keep most a11y at `warning`/`minor`; reserve `critical` for total page failure.)
- Cap nodes reported per rule (e.g. first 5) so one rule doesn't flood the report; note the total count in `detail`.

### 6. Report (`report.py`)
Two outputs into `outputs/<domain>-<timestamp>/`:
- **`results.json`** — full structured dump: meta (`url`, `pages_scanned`, `stopped_reason`, `duration`, counts by severity) + all findings. This is the machine-readable artifact Phase 3 will ingest.
- **`report.html`** — a single self-contained file (inline CSS, no external deps): summary cards (pages scanned, issues found, a health verdict), then findings **sorted by severity (critical first)**, grouped by check, each row showing title/detail/page. This is the shareable thing. Make it look clean — it's marketing.

### 7. CLI (`cli.py`)
`typer` app:
```
qascan scan URL [--max-pages N] [--max-depth N] [--timeout SECONDS] [--out DIR]
```
- Loads settings, builds `RunLimits`, runs the crawler+checks via `asyncio.run`, writes the report, prints a summary line and the path to `report.html`.
- Exit code 0 always for Phase 1 (it's an audit, not a gate) — but print counts.

### 8. (Optional, gated) Plain-English summary via `llm.py`
Only if time allows. Add `qascan/llm.py` with a single `summarize_findings(findings) -> str`. It receives the already-found findings and writes a 2–3 sentence plain summary. **Grounding rule applies:** it may only restate/summarize findings passed in — no new issues, no invented numbers. Temperature 0. This is the *only* LLM call in Phase 1, and the scanner must work fully without it. Skip if unsure.

---

## Acceptance criteria

Demonstrate each:

1. `qascan scan https://www.saucedemo.com --max-pages 20` completes within the time budget and writes `outputs/.../report.html` + `results.json`.
2. The crawler **never exceeds** `max_pages`, `max_depth`, or `time_budget` — show it stopping with the correct `stopped_reason` (test by setting `--max-pages 3`).
3. A deliberately broken target produces findings: point it at a page with a known broken image / dead link / console error and confirm each surfaces with correct severity. (Add a tiny local HTML fixture in `tests/fixtures/` with a broken `<img>` and a 404 link; assert the findings.)
4. axe-core runs and at least one accessibility finding appears on a page with a known violation (the fixture should include an `<img>` with no `alt` and a low-contrast element).
5. A single bad page (e.g. a URL that 500s) is reported as a `critical` finding and does **not** abort the rest of the scan.
6. `results.json` is valid and re-loadable; `Finding.id` is stable across two runs on the same input.
7. `ruff check .` and `pytest -q` green.

## Guardrails recap (don't violate)
- Zero LLM calls on the core path (the optional summary is the only exception, and it's grounded).
- Hard limits enforced and tested.
- Per-page errors become findings, never crashes.
- `report.html` is self-contained and presentable.