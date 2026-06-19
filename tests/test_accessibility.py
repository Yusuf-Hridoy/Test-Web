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

    rules = {f.detail.split("|")[0].strip() for f in findings}
    # Missing alt text and low-contrast are the planted violations.
    assert any("image-alt" in r for r in rules), f"expected image-alt, got {rules}"
    assert any("color-contrast" in r for r in rules), f"expected color-contrast, got {rules}"

    assert all(f.severity.value in ("warning", "minor") for f in findings)
