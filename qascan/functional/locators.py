"""Locator resolution + self-healing — the deterministic-first heart.

Order of resolution for an interactive step:
  1. Try ``step.selector`` deterministically. Exactly one visible element -> use it.
     (No LLM, no cache.)
  2. Failure/ambiguity + a cached heal for this step -> use the cached selector.
     (No LLM — this is why a re-run after a heal costs nothing.)
  3. Failure/ambiguity + ``step.hint`` -> ONE call to ``llm.relocate``. If confidence
     >= threshold and the proposed selector resolves: cache it, flag it for human
     review, use it.
  4. Otherwise raise ``StepFailed`` carrying evidence (screenshot + snapshot).

One LLM retry per step, maximum. Healed selectors are flagged for review and never
silently trusted long-term.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from .. import llm

DEFAULT_THRESHOLD = float(os.getenv("QASCAN_HEAL_THRESHOLD", "0.7"))


def _heal_cache_dir() -> Path:
    # Read at call time so tests can isolate the cache via QASCAN_CACHE_DIR.
    return Path(os.getenv("QASCAN_CACHE_DIR", ".qascan_cache")) / "heals"


@dataclass(frozen=True)
class StepRef:
    """Stable identity of a step within a suite — the heal-cache key."""

    suite_id: str
    case_id: str
    step_index: int

    @property
    def key(self) -> str:
        return f"{self.suite_id}|{self.case_id}|{self.step_index}"


class StepFailed(Exception):
    """A step could not be resolved/executed. Carries evidence, never a bare crash."""

    def __init__(self, message: str, *, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence or {}


class HealCache:
    """Persistent map of step -> healed selector, flagged for human review."""

    def __init__(self, suite_id: str, root: Path | None = None) -> None:
        self._path = (root or _heal_cache_dir()) / f"{suite_id}.json"
        self._data: dict[str, dict] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def get(self, ref: StepRef) -> str | None:
        entry = self._data.get(ref.key)
        return entry["selector"] if entry else None

    def put(self, ref: StepRef, selector: str, hint: str, confidence: float) -> None:
        self._data[ref.key] = {
            "selector": selector,
            "hint": hint,
            "confidence": confidence,
            "reviewed": False,  # flagged for human review until confirmed
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def healed_steps(self) -> list[dict]:
        """All cached heals (for the report's needs-review section)."""
        return [{"step": k, **v} for k, v in self._data.items()]

    def remove(self, step_key: str) -> bool:
        """Drop a cached heal so the step re-heals on the next run. Returns True
        if an entry was removed."""
        if step_key not in self._data:
            return False
        del self._data[step_key]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        return True


async def _resolves(page: Page, selector: str) -> Locator | None:
    """Return the locator if the selector matches exactly one visible element."""
    try:
        loc = page.locator(selector)
        count = await loc.count()
        if count == 1 and await loc.is_visible():
            return loc
    except PlaywrightError:
        return None
    return None


async def _snapshot(page: Page) -> str:
    """Bounded page snapshot for grounding the LLM."""
    from .snapshot import page_snapshot

    return await page_snapshot(page)


@dataclass
class Resolution:
    locator: Locator
    healed: bool


async def resolve(
    page: Page,
    step,
    ref: StepRef,
    heal_cache: HealCache,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> Resolution:
    """Resolve ``step`` to a Locator, healing via the LLM only as a last resort."""
    selector = step.selector
    if not selector:
        raise StepFailed(f"Step {ref.key} has no selector and cannot be resolved.")

    # 1. Deterministic happy path.
    loc = await _resolves(page, selector)
    if loc is not None:
        return Resolution(loc, healed=False)

    # 2. Cached heal — deterministic, no LLM.
    cached = heal_cache.get(ref)
    if cached:
        loc = await _resolves(page, cached)
        if loc is not None:
            return Resolution(loc, healed=True)

    # 3. One LLM heal attempt, if we have a hint to ground it.
    if step.hint:
        snapshot = await _snapshot(page)
        result = llm.relocate(snapshot, step.hint)
        if result.confidence >= threshold:
            loc = await _resolves(page, result.selector)
            if loc is not None:
                heal_cache.put(ref, result.selector, step.hint, result.confidence)
                return Resolution(loc, healed=True)
        evidence = {"snapshot": snapshot, "llm_selector": result.selector,
                    "confidence": result.confidence}
        raise StepFailed(
            f"Could not heal step {ref.key} (confidence {result.confidence:.2f}).",
            evidence=evidence,
        )

    # 4. No hint, or low confidence.
    raise StepFailed(
        f"Selector '{selector}' did not resolve for step {ref.key} and no hint is set."
    )
