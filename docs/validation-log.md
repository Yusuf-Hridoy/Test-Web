# Real-world validation log (Phase 5, Task 1)

Goal: scan a spread of real public sites, triage findings by hand, and drive down
false positives until a report could be sent to the site owner unedited.

Method: `crawl()` with `max_pages=5, max_depth=1, time_budget=60s` per site.
(Reproduce with `python scripts/validate.py` — or the snippet at the bottom.)

## Sites scanned (8, varied types)

| site | type | pages | stop | notable finding types |
|------|------|------:|------|------------------------|
| example.com | minimal | 2 | completed | wcag×2 |
| iana.org | docs | 5 | max_pages | wcag×15, link_blocked×15, link_unverified×4, 3p-console×2 |
| books.toscrape.com | e-commerce demo | 5 | max_pages | wcag×16, console_error×4 |
| quotes.toscrape.com | server-rendered | 5 | max_pages | wcag×14, link_blocked×1 |
| quotes.toscrape.com/js | JS-heavy SPA | 5 | completed | wcag×17, link_blocked×1 |
| saucedemo.com | login app | 2 | completed | wcag×3, 3p-console×2 |
| news.ycombinator.com | large/linky | 5 | max_pages | wcag×21, link_blocked×271, broken_link×2, non_html×1 |
| wikipedia.org | large | 6 | max_pages | wcag×8, link_blocked×308 |

## False-positive sources found and fixed

1. **Rate-limited / bot-blocked external links read as "broken."**
   This was the biggest source. HN (271) and Wikipedia (308) return 403/429 to a
   bot user-agent for outbound links. Pre-fix these would all be `broken_link`
   *warnings* — ~580 false alarms across two sites alone.
   **Fix:** 401/403/429 → `link_blocked` (severity **info**, "could not verify"),
   and connection errors/timeouts → `link_unverified` (info). Only 404/410/4xx/5xx
   remain `broken_link` (warning). After the fix, real broken links across all 8
   sites: **2** (both genuine, on HN).

2. **Third-party script console errors blamed on the site.**
   iana.org and saucedemo emit console errors from analytics/embeds. Pre-fix these
   were first-party `console_error` warnings.
   **Fix:** errors whose source URL is a different registrable domain → 
   `console_error_thirdparty` (severity **minor**, source noted). First-party
   console errors (e.g. books.toscrape ×4) stay warnings.

3. **Crawler trap: query-param explosion.** Faceted/paginated links (`?page=N`,
   filters) can exhaust the page budget on one logical page.
   **Fix:** cap distinct query-variants per path (5); see `urls.path_key`.

4. **Redirects & trailing-slash duplicates double-counted.**
   **Fix:** trailing slash normalized away in `normalize_url`; after navigation the
   *final* (post-redirect) URL is de-duplicated against `visited`.

5. **Non-HTML links (PDF/zip/images) crawled as pages.** A same-domain PDF link
   would navigate → trigger a download error → bogus `critical` page_error
   (HN surfaced one). **Fix:** `looks_non_html()` skips navigation by extension and
   records an `info` `non_html` finding; such links are still liveness-checked.

## Outcome / exit signal

After the fixes, the warning/critical surface on these sites is dominated by
**real** issues (accessibility violations from axe, a couple of genuinely broken
links, first-party console errors). The high-volume noise (blocked external links,
third-party script errors) is now **info/minor** and clearly labelled "could not
verify" — so a report is sendable to the owner without manual scrubbing.

Residual judgement calls (documented, not bugs):
- axe accessibility counts are real but high on content-heavy sites; severity is
  capped at warning/minor so they don't read as outages.
- `link_blocked` is intentionally *surfaced* (info) rather than hidden, so an owner
  can see what we couldn't verify — transparency over silent omission.

---

# Phase 6 — new check types validation

Each new type gets the same triage discipline before being called done.

## SEO (default-on, oracle-free)

Sampled `seo`-only over example.com, books.toscrape.com, iana.org, quotes.toscrape.com
(4 pages each):

| finding type | severity | assessment |
|--------------|----------|------------|
| seo_meta_description_missing | minor | true positive — these pages genuinely lack it |
| seo_canonical_missing | info | true but soft (not every page needs canonical) → **info**, not a warning |
| seo_heading_order | minor | true positive — real h-level jumps (e.g. books.toscrape) |
| seo_title_duplicate | minor | true positive — paginated listings reuse titles |
| seo_sitefile_missing | info | robots.txt / sitemap.xml absent → **info** (advisory) |

False-positive controls applied:
- **No alt double-report.** `seo_img_alt_missing` is suppressed when the accessibility
  check is on (axe owns `image-alt`); verified by `tests/test_seo.py`.
- Soft/contextual rules (canonical, sitemap, long title) are **info**, so they never
  inflate the health verdict — only genuinely actionable items (missing title/description,
  missing/duplicate h1) are minor/warning.

## Performance (opt-in, Lighthouse)

Verified against real sites: example.com & wikipedia.org scored within "good" → **zero**
findings (a fast site stays clean — no crying wolf). quotes.toscrape.com/js produced
`performance_score` 85 (minor) and `LCP 3085ms` (minor), matching its real Lighthouse run.

FP controls: metrics within Core-Web-Vitals "good" thresholds emit **no** finding; only
"needs improvement"/"poor" do. Requires Node + `lighthouse`; if absent the check emits a
single INFO "unavailable" item rather than failing the scan.

## API security (SecureQA)

Deferred: the SecureQA Orchestrator work isn't present in this repo to fold in. The
`Finding`/report plumbing is ready for it (any new type just emits `Finding`s); shipping
it is a follow-up when that code is available.

```python
# scripts/validate.py
import asyncio, collections
from qascan.crawler import crawl
from qascan.config import RunLimits
SITES = ["https://example.com","https://www.iana.org","https://books.toscrape.com",
         "https://quotes.toscrape.com","https://quotes.toscrape.com/js/",
         "https://www.saucedemo.com","https://news.ycombinator.com","https://www.wikipedia.org"]
async def main():
    for url in SITES:
        r = await crawl(url, RunLimits(max_pages=5, max_depth=1, time_budget_seconds=60))
        print(url, r.pages_scanned, collections.Counter(f.type for f in r.findings))
asyncio.run(main())
```
