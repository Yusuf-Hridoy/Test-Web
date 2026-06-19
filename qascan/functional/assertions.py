"""Assertions: deterministic first, fuzzy ``verify_nl`` only when needed.

Deterministic assertions (``visible``/``text``/``url``/``count``) give a hard
pass/fail and never touch the LLM. ``verify_nl`` is the escape hatch for things
deterministic checks can't express; its verdict is grounded in the page snapshot
and always surfaced as "needs review" — distinct from deterministic results.
"""

from __future__ import annotations

import re

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from pydantic import BaseModel

from .. import llm
from .snapshot import page_snapshot


class AssertionOutcome(BaseModel):
    kind: str  # visible | text | url | count | verify_nl
    deterministic: bool
    passed: bool | None  # None => unsure (verify_nl only)
    needs_review: bool
    message: str
    confidence: float | None = None
    provenance: str | None = None


async def _deterministic(page: Page, step) -> AssertionOutcome:
    kind = step.assert_
    selector = step.selector
    expected = step.value

    if kind == "url":
        url = page.url
        ok = bool(re.search(expected, url)) if expected else False
        return AssertionOutcome(
            kind="url", deterministic=True, passed=ok, needs_review=False,
            message=f"URL {'matched' if ok else 'did not match'} /{expected}/ (was {url}).",
        )

    if not selector:
        return AssertionOutcome(
            kind=kind or "unknown", deterministic=True, passed=False, needs_review=False,
            message=f"Assertion '{kind}' requires a selector.",
        )
    loc = page.locator(selector)

    try:
        if kind == "count":
            actual = await loc.count()
            want = int(expected) if expected is not None else -1
            ok = actual == want
            return AssertionOutcome(
                kind="count", deterministic=True, passed=ok, needs_review=False,
                message=f"Expected {want} element(s) for '{selector}', found {actual}.",
            )
        if kind == "visible":
            ok = (await loc.count()) >= 1 and await loc.first.is_visible()
            return AssertionOutcome(
                kind="visible", deterministic=True, passed=ok, needs_review=False,
                message=f"Element '{selector}' is {'visible' if ok else 'not visible'}.",
            )
        if kind == "text":
            if await loc.count() == 0:
                return AssertionOutcome(
                    kind="text", deterministic=True, passed=False, needs_review=False,
                    message=f"No element matched '{selector}' for text assertion.",
                )
            actual = (await loc.first.inner_text()).strip()
            ok = expected is not None and (actual == expected or expected in actual)
            return AssertionOutcome(
                kind="text", deterministic=True, passed=ok, needs_review=False,
                message=f"Text for '{selector}' was {actual!r}; expected {expected!r}.",
            )
    except PlaywrightError as exc:
        return AssertionOutcome(
            kind=kind or "unknown", deterministic=True, passed=False, needs_review=False,
            message=f"Assertion error on '{selector}': {str(exc).splitlines()[0]}",
        )

    return AssertionOutcome(
        kind=str(kind), deterministic=True, passed=False, needs_review=False,
        message=f"Unknown assertion kind: {kind!r}.",
    )


async def _verify_nl(page: Page, step, threshold: float) -> AssertionOutcome:
    expectation = step.value or step.hint or ""
    snapshot = await page_snapshot(page)
    result = llm.verify(snapshot, expectation)
    # verify_nl is ALWAYS surfaced as needs-review; low confidence / unsure => [VERIFY].
    unsure = result.passed is None or result.confidence < threshold
    return AssertionOutcome(
        kind="verify_nl",
        deterministic=False,
        passed=result.passed,
        needs_review=True,
        confidence=result.confidence,
        provenance=llm.VERIFY if unsure else result.provenance,
        message=f"verify_nl: {result.reason}",
    )


async def evaluate(page: Page, step, *, threshold: float = 0.7) -> AssertionOutcome:
    """Evaluate an ``expect`` or ``verify_nl`` step into an outcome."""
    if step.action == "verify_nl":
        return await _verify_nl(page, step, threshold)
    return await _deterministic(page, step)
