"""Plain-instruction -> TestCase generator (compile-once).

``qascan generate URL "instruction"`` explores the page ONCE, asks the LLM to draft
a TestCase, and writes an editable Suite YAML marked ``source="generated"``. The
case then executes deterministically like any written case — we never re-plan on
run, so planning cost is one-time and regression validity is preserved.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .. import llm
from .schema import Step, Suite, TargetConfig, TestCase
from .snapshot import page_snapshot


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:50] or "generated-case"


async def _explore(url: str) -> str:
    """One-time page exploration: a bounded page snapshot."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_load_state("load", timeout=5_000)
            return await page_snapshot(page)
        except PlaywrightError:
            return ""
        finally:
            await browser.close()


async def generate(url: str, instruction: str, out_path: str | Path) -> tuple[Suite, Path]:
    """Draft a TestCase from an instruction and write a runnable Suite YAML."""
    snapshot = await _explore(url)
    data = llm.generate_case(snapshot, instruction, base_url=url)

    steps = [Step.model_validate(s) for s in data.get("steps", [])]
    name = data.get("name") or instruction
    case = TestCase(id=_slug(name), name=name, steps=steps, source="generated")
    suite = Suite(name=_slug(name), target=TargetConfig(base_url=url), cases=[case])

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(suite.to_yaml(), encoding="utf-8")
    return suite, out_path
