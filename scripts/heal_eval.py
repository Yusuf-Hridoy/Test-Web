"""Self-healing accuracy benchmark (Phase 5, Task 2).

Runs ``llm.relocate`` over a fixed set of (page, broken selector, hint, correct
target) cases and reports heal success rate, FALSE-heal rate (relocated to the
WRONG element — the dangerous failure), and confidence on hits vs misses.

The correct element on each page carries a ``data-truth`` attribute, so we can
verify the proposed selector resolves to the intended element rather than just
*some* element.

    python scripts/heal_eval.py            # prints a report, writes docs/eval-results.md

Deterministic fixtures + a real Gemini key. Kept as a regression guard for the
prompt/snapshot format. NOT part of the pytest suite (it needs network).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from qascan import llm
from qascan.functional.snapshot import page_snapshot

THRESHOLD = 0.7  # chosen heal-acceptance confidence (see report)

# Each case: a page, a deliberately-wrong selector, an NL hint, and the correct
# element marked with data-truth. "difficulty" is just for the report.
CASES = [
    {
        "id": "login_button", "difficulty": "easy",
        "hint": "the green Login button that submits the login form",
        "broken": "#login-x",
        "html": """
            <form><input id="user"><input id="pass" type="password">
            <button data-truth type="submit" style="background:green">Login</button></form>
            <a href="/help">Need help?</a>""",
    },
    {
        "id": "search_box", "difficulty": "easy",
        "hint": "the site search input box",
        "broken": "input#search",
        "html": """
            <input name="email" placeholder="email">
            <input data-truth name="q" type="search" placeholder="Search the site">
            <input name="qty" type="number">""",
    },
    {
        "id": "pricing_nav", "difficulty": "easy",
        "hint": "the Pricing link in the top navigation",
        "broken": "nav a.pricing",
        "html": """<nav><a href="/features">Features</a>
            <a data-truth href="/pricing">Pricing</a><a href="/docs">Docs</a></nav>""",
    },
    {
        "id": "accept_terms", "difficulty": "medium",
        "hint": "the checkbox to accept the terms and conditions",
        "broken": "#tos",
        "html": """<label><input type="checkbox" name="news"> Newsletter</label>
            <label><input data-truth type="checkbox" name="terms"> I accept the Terms</label>""",
    },
    {
        "id": "country_select", "difficulty": "medium",
        "hint": "the country selection dropdown",
        "broken": "select#country",
        "html": """<select name="lang"><option>EN</option></select>
            <select data-truth name="country"><option>USA</option><option>UK</option></select>""",
    },
    {
        "id": "add_to_cart_specific", "difficulty": "hard",
        "hint": "the Add to cart button for the Blue Widget",
        "broken": "#buy-blue",
        "html": """
            <div class="product"><h3>Red Widget</h3><button>Add to cart</button></div>
            <div class="product"><h3>Blue Widget</h3>
              <button data-truth>Add to cart</button></div>
            <div class="product"><h3>Green Widget</h3><button>Add to cart</button></div>""",
    },
    {
        "id": "contact_submit", "difficulty": "hard",
        "hint": "the Submit button of the contact form (not the newsletter form)",
        "broken": "#contact-send",
        "html": """
            <form id="newsletter"><input><button>Submit</button></form>
            <form id="contact"><textarea></textarea>
              <button data-truth>Submit</button></form>""",
    },
    {
        "id": "delete_item2", "difficulty": "hard",
        "hint": "the Delete button for row 2 in the table",
        "broken": "tr[data-row='2'] .del",
        "html": """<table><tr data-row="1"><td>A</td>
              <td><button>Edit</button><button>Delete</button></td></tr>
            <tr data-row="2"><td>B</td>
              <td><button>Edit</button><button data-truth>Delete</button></td></tr></table>""",
    },
]


async def _resolution(page, selector: str) -> str:
    """Classify what `selector` resolves to: 'truth' | 'wrong' | 'none' | 'ambiguous'."""
    try:
        loc = page.locator(selector)
        count = await loc.count()
    except Exception:  # noqa: BLE001 — bad selector syntax from the model
        return "none"
    if count == 0:
        return "none"
    if count > 1:
        return "ambiguous"
    try:
        is_truth = await loc.evaluate("el => el.hasAttribute('data-truth')")
    except Exception:  # noqa: BLE001
        return "none"
    return "truth" if is_truth else "wrong"


async def run() -> dict:
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        for case in CASES:
            await page.set_content(f"<!doctype html><html><body>{case['html']}</body></html>")
            snapshot = await page_snapshot(page)
            heal = llm.relocate(snapshot, case["hint"])
            res = await _resolution(page, heal.selector)
            results.append({**case, "selector": heal.selector,
                            "confidence": heal.confidence, "resolution": res})
        await browser.close()
    return _summarize(results)


def _classify(r: dict, threshold: float) -> str:
    if r["confidence"] < threshold:
        return "miss"  # rejected by threshold -> clean failure
    if r["resolution"] == "truth":
        return "success"
    if r["resolution"] == "wrong":
        return "false_heal"  # accepted a WRONG element -> dangerous
    return "miss"  # none / ambiguous, accepted but unusable -> clean failure


def _summarize(results: list[dict]) -> dict:
    n = len(results)
    labels = [_classify(r, THRESHOLD) for r in results]
    succ = labels.count("success")
    false_heals = labels.count("false_heal")
    hit_conf = [r["confidence"] for r, lab in zip(results, labels, strict=True)
                if lab == "success"]
    miss_conf = [r["confidence"] for r, lab in zip(results, labels, strict=True)
                 if lab != "success"]
    return {
        "results": results, "labels": labels, "n": n,
        "success_rate": succ / n, "false_heal_rate": false_heals / n,
        "avg_conf_hit": sum(hit_conf) / len(hit_conf) if hit_conf else 0.0,
        "avg_conf_miss": sum(miss_conf) / len(miss_conf) if miss_conf else 0.0,
    }


def _write_report(s: dict) -> None:
    n = s["n"]
    n_succ = s["labels"].count("success")
    n_false = s["labels"].count("false_heal")
    n_miss = s["labels"].count("miss")
    lines = [
        "# Self-healing accuracy benchmark", "",
        f"Eval set: **{s['n']}** cases (easy → hard, deterministic local fixtures). "
        f"Harness: `scripts/heal_eval.py`. Model: `gemini-2.5-flash-lite`, temperature 0.", "",
        f"**Chosen confidence threshold: {THRESHOLD}.** A missed heal is a clean failure; "
        "a false-heal (relocating to the *wrong* element) is a lie, so the threshold is "
        "tuned to keep false-heals near zero.", "",
        "## Results at the chosen threshold", "",
        f"- Heal success rate: **{s['success_rate']:.0%}** ({n_succ}/{n})",
        f"- **False-heal rate: {s['false_heal_rate']:.0%}** ({n_false}/{n})",
        f"- Misses (clean failures): {n_miss}/{n}",
        f"- Avg confidence — hits: {s['avg_conf_hit']:.2f} · non-hits: {s['avg_conf_miss']:.2f}",
        "", "## Per-case", "",
        "| case | difficulty | conf | resolved | outcome |",
        "|------|-----------|------|----------|---------|",
    ]
    for r, label in zip(s["results"], s["labels"], strict=True):
        lines.append(f"| {r['id']} | {r['difficulty']} | {r['confidence']:.2f} | "
                     f"{r['resolution']} | {label} |")
    lines += ["", "## Reading this",
              "- **success** — accepted (conf ≥ threshold) and resolved to the intended element.",
              "- **false_heal** — accepted but resolved to the WRONG element. This is the "
              "dangerous outcome; the threshold exists to drive it to zero.",
              "- **miss** — rejected by the threshold, or resolved to nothing/ambiguous. "
              "Surfaces as a clean StepFailed, not a wrong pass.", ""]
    out = Path("docs/eval-results.md")
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    summary = asyncio.run(run())
    print(f"success={summary['success_rate']:.0%} "
          f"false_heal={summary['false_heal_rate']:.0%} (threshold {THRESHOLD})")
    _write_report(summary)
