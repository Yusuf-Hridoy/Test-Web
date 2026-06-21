"""axe-core accessibility check against the broken fixture (criterion 4)."""

from __future__ import annotations

from qascan.checks import accessibility


async def test_axe_finds_violations(page, http_server):
    url = f"{http_server}/broken.html"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")
    findings = await accessibility.run_axe(page, url)

    assert findings, "axe should report at least one violation on the broken fixture"
    assert all(f.type == "wcag_violation" and f.check == "accessibility" for f in findings)

    # Structured metadata (no pipe-delimited detail); rule ids live in meta.
    rules = {f.meta["rule_id"] for f in findings}
    assert any("image-alt" in r for r in rules), f"expected image-alt, got {rules}"
    assert any("color-contrast" in r for r in rules), f"expected color-contrast, got {rules}"

    # Severity is driven by axe impact and must match what meta records.
    impact_to_sev = {"critical": "critical", "serious": "warning",
                     "moderate": "minor", "minor": "minor"}
    for f in findings:
        assert f.severity.value == impact_to_sev[f.meta["impact"]]
        assert f.meta["help_url"]  # "Learn more" link is present
