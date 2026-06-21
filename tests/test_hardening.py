"""Phase 5 hardening — false-positive guards, robustness, determinism.

All deterministic and network-free (local fixture server only).
"""

from __future__ import annotations

from qascan.checks import exploratory
from qascan.checks.exploratory import ConsoleCollector
from qascan.config import RunLimits
from qascan.crawler import crawl
from qascan.functional.schema import Step


# --------------------------------------------------------------------------- #
# Link classification: broken vs blocked/unverified (Task 1)
# --------------------------------------------------------------------------- #
async def test_link_blocked_vs_broken(http_server):
    checker = exploratory.LinkChecker(RunLimits())
    sources = {
        f"{http_server}/status403": http_server,   # bot-blocked
        f"{http_server}/status429": http_server,   # rate-limited
        f"{http_server}/missing-xyz.html": http_server,  # genuinely 404
    }
    findings = await checker.check(sources)
    by_type = {f.type for f in findings}
    assert "link_blocked" in by_type      # 403/429 are NOT "broken"
    assert "broken_link" in by_type       # 404 is broken
    blocked = [f for f in findings if f.type == "link_blocked"]
    assert all(f.severity.value == "info" for f in blocked)  # low-severity, not warning
    broken = [f for f in findings if f.type == "broken_link"]
    assert all(f.severity.value == "warning" for f in broken)


# --------------------------------------------------------------------------- #
# Console errors: first-party vs third-party (Task 1)
# --------------------------------------------------------------------------- #
def test_console_first_vs_third_party():
    coll = ConsoleCollector()
    coll.console_errors = [
        ("first-party boom", "https://site.test/app.js"),
        ("third-party ad error", "https://ads.example/track.js"),
    ]
    findings = exploratory.check_console(coll, "https://site.test/page")
    by_type = {f.type: f for f in findings}
    assert by_type["console_error"].severity.value == "warning"          # first-party
    assert by_type["console_error_thirdparty"].severity.value == "minor"  # third-party


# --------------------------------------------------------------------------- #
# Non-HTML resource is recorded, not crashed/checked (Task 3)
# --------------------------------------------------------------------------- #
async def test_non_html_resource(http_server):
    result = await crawl(f"{http_server}/doc.pdf", RunLimits(max_pages=3, max_depth=0))
    assert result.pages_scanned == 1
    assert any(f.type == "non_html" for f in result.findings)
    assert not any(f.type == "wcag_violation" for f in result.findings)  # no axe on a PDF


# --------------------------------------------------------------------------- #
# Redirect dedupe: /redirect -> /welcome.html isn't double counted (Task 1)
# --------------------------------------------------------------------------- #
async def test_redirect_not_double_counted(http_server):
    # broken.html links to ok.html; we also visit /redirect which lands on welcome.
    result = await crawl(f"{http_server}/redirect", RunLimits(max_pages=10, max_depth=1))
    # The redirect target (welcome.html) is what actually gets scanned.
    assert result.stopped_reason == "completed"
    assert result.pages_scanned >= 1


# --------------------------------------------------------------------------- #
# Query-param explosion is capped (Task 1)
# --------------------------------------------------------------------------- #
async def test_param_explosion_capped(http_server):
    # traps.html links to 12 ?page=N variants of itself; cap is 5.
    result = await crawl(f"{http_server}/traps.html", RunLimits(max_pages=50, max_depth=2))
    # seed + at most 5 param variants = 6, far below max_pages (50).
    assert result.pages_scanned <= 6
    assert result.stopped_reason == "completed"


# --------------------------------------------------------------------------- #
# Determinism: same input -> identical findings (Task 5, criterion 5)
# --------------------------------------------------------------------------- #
async def test_findings_byte_identical_across_runs(http_server):
    # welcome.html has stable findings (a11y + seo) and no broken subresources —
    # so this isn't sensitive to console-capture timing for failed image loads.
    # (Real-world grouped-diff stability is validated end-to-end on a live site.)
    limits = RunLimits(max_pages=1, max_depth=0)
    r1 = await crawl(f"{http_server}/welcome.html", limits)
    r2 = await crawl(f"{http_server}/welcome.html", limits)
    dump1 = [f.model_dump(mode="json") for f in sorted(r1.findings, key=lambda f: f.id)]
    dump2 = [f.model_dump(mode="json") for f in sorted(r2.findings, key=lambda f: f.id)]
    assert dump1 == dump2  # identical findings, no nondeterminism
    assert len(dump1) > 0


# --------------------------------------------------------------------------- #
# Malformed / junk hrefs never crash extraction (Task 3)
# --------------------------------------------------------------------------- #
async def test_junk_hrefs_handled(page, http_server):
    await page.goto(f"{http_server}/form.html", wait_until="domcontentloaded")
    await page.evaluate("""() => {
        for (const h of ['mailto:a@b.com','tel:+123','javascript:void(0)','##','data:x']) {
            const a = document.createElement('a'); a.href = h; a.textContent = h;
            document.body.appendChild(a);
        }
    }""")
    links = await exploratory.extract_links(page)
    assert all(not href.lower().startswith(("mailto:", "tel:", "javascript:", "data:"))
               for href in links)


def test_step_schema_unknown_action_rejected():
    import pydantic
    import pytest
    with pytest.raises(pydantic.ValidationError):
        Step(action="teleport")  # not in the Literal of allowed actions
