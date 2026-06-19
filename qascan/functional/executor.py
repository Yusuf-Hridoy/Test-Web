"""Step executor: deterministic-first run of a Suite with self-healing fallback.

Each step becomes a StepResult; each case a pass/fail (or needs-review). A failing
step records evidence and either stops the case or continues. Errors are results,
never crashes. A Playwright trace + screenshots are captured for every run.
"""

from __future__ import annotations

import time
from datetime import UTC
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel

from . import assertions, auth
from .locators import HealCache, Resolution, StepRef, resolve
from .schema import Suite, TestCase


class StepResult(BaseModel):
    index: int
    action: str
    status: str  # pass | fail | needs_review | skipped
    message: str = ""
    healed: bool = False
    selector_used: str | None = None
    evidence: str | None = None  # screenshot path
    confidence: float | None = None


class CaseResult(BaseModel):
    id: str
    name: str
    source: str
    status: str  # pass | fail | needs_review
    steps: list[StepResult]
    duration_seconds: float


class RunResult(BaseModel):
    suite_name: str
    base_url: str
    status: str  # pass | fail | needs_review | session_expired
    message: str = ""
    cases: list[CaseResult] = []
    healed_for_review: list[dict] = []
    llm_calls: int = 0
    duration_seconds: float = 0.0


def _case_status(steps: list[StepResult]) -> str:
    if any(s.status == "fail" for s in steps):
        return "fail"
    if any(s.status == "needs_review" for s in steps):
        return "needs_review"
    return "pass"


async def _screenshot(page, evidence_dir: Path, case_id: str, index: int) -> str | None:
    """Capture a failure screenshot; return its path (or None if it can't be taken)."""
    shot = evidence_dir / f"{case_id}-step{index}.png"
    try:
        await page.screenshot(path=str(shot))
        return str(shot)
    except PlaywrightError:
        return None


async def _run_action(page, step, res: Resolution | None) -> None:
    """Perform a non-assertion action. Raises PlaywrightError on failure."""
    action = step.action
    if action == "click":
        await res.locator.click()
    elif action == "fill":
        await res.locator.fill(step.value or "")
    elif action == "select":
        await res.locator.select_option(step.value or "")
    elif action == "wait":
        if step.value and step.value.replace(".", "", 1).isdigit():
            await page.wait_for_timeout(float(step.value) * 1000)
        else:
            await page.wait_for_load_state("networkidle")


async def _run_case(
    page, case: TestCase, suite: Suite, heal_cache: HealCache, *,
    continue_on_fail: bool, threshold: float, evidence_dir: Path,
) -> CaseResult:
    started = time.monotonic()
    steps: list[StepResult] = []
    stopped = False

    for i, step in enumerate(case.steps):
        if stopped:
            steps.append(StepResult(index=i, action=step.action, status="skipped",
                                    message="Skipped after an earlier failure."))
            continue

        ref = StepRef(suite_id=suite.name, case_id=case.id, step_index=i)
        try:
            if step.action == "goto":
                url = urljoin(suite.target.base_url, step.target or "")
                await page.goto(url, wait_until="domcontentloaded")
                steps.append(StepResult(index=i, action="goto", status="pass",
                                        message=f"Navigated to {url}."))
            elif step.action in ("expect", "verify_nl"):
                outcome = await assertions.evaluate(page, step, threshold=threshold)
                status = (
                    "needs_review" if outcome.needs_review
                    else ("pass" if outcome.passed else "fail")
                )
                shot_path = None
                if status == "fail":  # screenshot on every failure
                    shot_path = await _screenshot(page, evidence_dir, case.id, i)
                steps.append(StepResult(
                    index=i, action=step.action, status=status, message=outcome.message,
                    confidence=outcome.confidence, evidence=shot_path,
                ))
                if status == "fail" and not continue_on_fail:
                    stopped = True
            else:  # interactive: click/fill/select/wait
                res = None
                if step.action != "wait":
                    res = await resolve(page, step, ref, heal_cache, threshold=threshold)
                await _run_action(page, step, res)
                steps.append(StepResult(
                    index=i, action=step.action,
                    status="pass",
                    healed=bool(res and res.healed),
                    selector_used=step.selector,
                    message="healed via fallback" if (res and res.healed) else "ok",
                ))
        except Exception as exc:  # noqa: BLE001 — errors are results, never crashes
            shot_path = await _screenshot(page, evidence_dir, case.id, i)
            steps.append(StepResult(
                index=i, action=step.action, status="fail",
                message=str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                evidence=shot_path,
            ))
            if not continue_on_fail:
                stopped = True

    return CaseResult(
        id=case.id, name=case.name, source=case.source,
        status=_case_status(steps), steps=steps,
        duration_seconds=round(time.monotonic() - started, 3),
    )


async def run_suite(
    suite: Suite, *, out_root: str | Path = "outputs",
    continue_on_fail: bool = False, threshold: float = 0.7,
) -> tuple[RunResult, Path]:
    """Run every case in the suite and write a report. Returns (result, out_dir)."""
    from datetime import datetime

    from .. import llm
    from ..report import write_functional_report

    llm.reset_call_count()
    started = time.monotonic()
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safe = suite.name.replace(" ", "_").replace("/", "_")
    out_dir = Path(out_root) / f"suite-{safe}-{ts}"
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    heal_cache = HealCache(suite.name)
    result = RunResult(suite_name=suite.name, base_url=suite.target.base_url, status="pass")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(**auth.build_context_kwargs(suite.target.auth))
        await context.tracing.start(screenshots=True, snapshots=True)
        try:
            # Fail fast on an expired session — no flood of false failures.
            try:
                await auth.assert_session_valid(context, suite.target.auth, suite.target.base_url)
            except auth.SessionExpired as exc:
                result.status = "session_expired"
                result.message = str(exc)
                result.healed_for_review = heal_cache.healed_steps()
                result.duration_seconds = round(time.monotonic() - started, 3)
                write_functional_report(result, out_dir)
                return result, out_dir  # finally block still closes context/browser

            page = await context.new_page()
            for case in suite.cases:
                result.cases.append(await _run_case(
                    page, case, suite, heal_cache,
                    continue_on_fail=continue_on_fail, threshold=threshold,
                    evidence_dir=evidence_dir,
                ))
        finally:
            try:
                await context.tracing.stop(path=str(out_dir / "trace.zip"))
            except PlaywrightError:
                pass
            await context.close()
            await browser.close()

    if any(c.status == "fail" for c in result.cases):
        result.status = "fail"
    elif any(c.status == "needs_review" for c in result.cases):
        result.status = "needs_review"

    result.healed_for_review = heal_cache.healed_steps()
    result.llm_calls = llm.live_call_count()
    result.duration_seconds = round(time.monotonic() - started, 3)

    write_functional_report(result, out_dir)
    return result, out_dir
