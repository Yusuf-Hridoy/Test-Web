"""The single LLM touchpoint (Gemini).

No other module imports the Gemini SDK or builds prompts. One low-level function
(:func:`_invoke`) actually talks to Gemini; everything else builds a grounded,
JSON-only prompt, checks the on-disk cache, and parses the result into a pydantic
model. Tests monkeypatch :func:`_invoke` to run the whole stack without a key.

Rules enforced here (CLAUDE.md):
- Temperature 0, JSON-only responses, parsed into pydantic.
- Every result cached by a stable key (same input -> same output, no re-pay).
- Grounding: outputs are tagged DERIVED / COMPUTED / [VERIFY]; the model returns
  [VERIFY] (surfaced to the user) instead of guessing.
- A run with no failures makes ZERO live calls — callers only reach the LLM on a
  deterministic miss, and cache hits never count as live calls.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

MODEL = "gemini-2.5-flash-lite"
TEMPERATURE = 0


def _cache_dir() -> Path:
    # Read at call time so tests can isolate the cache via QASCAN_CACHE_DIR.
    return Path(os.getenv("QASCAN_CACHE_DIR", ".qascan_cache")) / "llm"

# Provenance tags (grounding).
DERIVED = "DERIVED"
COMPUTED = "COMPUTED"
VERIFY = "[VERIFY]"

# Live Gemini calls this process — cache hits do NOT increment. Tests assert 0.
_live_calls = 0


class LLMUnavailable(RuntimeError):
    """Raised when an LLM call is required but no API key is configured."""


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class RelocateResult(BaseModel):
    selector: str
    confidence: float
    provenance: str = DERIVED


class VerifyResult(BaseModel):
    passed: bool | None = None  # None => model is unsure -> needs review
    reason: str
    confidence: float
    provenance: str = DERIVED


# --------------------------------------------------------------------------- #
# Call accounting (for tests / "zero LLM calls on clean run" guarantee)
# --------------------------------------------------------------------------- #
def live_call_count() -> int:
    return _live_calls


def reset_call_count() -> None:
    global _live_calls
    _live_calls = 0


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _cache_key(fn: str, payload: dict[str, Any]) -> str:
    blob = json.dumps({"fn": fn, "payload": payload}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    path = _cache_dir() / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _cache_put(key: str, value: dict) -> None:
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# The ONE function that talks to Gemini (monkeypatched in tests)
# --------------------------------------------------------------------------- #
def _invoke(prompt: str, schema: dict) -> dict:
    """Send ``prompt`` to Gemini and return parsed JSON. Increments live count."""
    global _live_calls
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise LLMUnavailable(
            "GEMINI_API_KEY is not set. Add it to .env to enable LLM features "
            "(self-healing, verify_nl, generate)."
        )
    from google import genai  # local import: importing this module needs no key
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=TEMPERATURE,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    _live_calls += 1
    return json.loads(response.text)


def _cached_invoke(fn: str, payload: dict, prompt: str, schema: dict) -> dict:
    """Cache-wrapped invoke. A cache hit never reaches :func:`_invoke`."""
    key = _cache_key(fn, payload)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _invoke(prompt, schema)
    _cache_put(key, result)
    return result


# --------------------------------------------------------------------------- #
# Public, grounded operations
# --------------------------------------------------------------------------- #
_GROUNDING = (
    "You may only use facts present in the SNAPSHOT below. Do not invent selectors, "
    "text, numbers, or results. If you cannot ground your answer in the snapshot, set "
    'confidence low and provenance to "[VERIFY]". Respond with JSON only.'
)


def relocate(snapshot: str, hint: str) -> RelocateResult:
    """Given a page snapshot and a natural-language hint, propose a corrected
    Playwright selector for the intended element, with a confidence in [0,1]."""
    prompt = (
        "A Playwright selector failed. Find the element described by the HINT in the "
        "page SNAPSHOT and return a single, robust Playwright selector for it.\n\n"
        f"{_GROUNDING}\n\n"
        f"HINT: {hint}\n\nSNAPSHOT:\n{snapshot}\n"
    )
    schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "confidence": {"type": "number"},
            "provenance": {"type": "string"},
        },
        "required": ["selector", "confidence"],
    }
    data = _cached_invoke("relocate", {"hint": hint, "snapshot": snapshot}, prompt, schema)
    return RelocateResult(**data)


def verify(snapshot: str, expectation: str) -> VerifyResult:
    """Fuzzy assertion: does the page snapshot satisfy a natural-language
    expectation? Returns a verdict, a grounded reason, and a confidence.
    ``passed=None`` means the model is unsure (surface as needs-review)."""
    prompt = (
        "Decide whether the page SNAPSHOT satisfies the EXPECTATION. Cite the specific "
        "snapshot content that supports your verdict in 'reason'. If the evidence is "
        "ambiguous or absent, set passed=null and explain.\n\n"
        f"{_GROUNDING}\n\n"
        f"EXPECTATION: {expectation}\n\nSNAPSHOT:\n{snapshot}\n"
    )
    schema = {
        "type": "object",
        "properties": {
            # nullable: the model returns null when it cannot decide (-> needs review).
            "passed": {"type": "boolean", "nullable": True},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
            "provenance": {"type": "string"},
        },
        "required": ["reason", "confidence"],
    }
    data = _cached_invoke(
        "verify", {"expectation": expectation, "snapshot": snapshot}, prompt, schema
    )
    return VerifyResult(**data)


def generate_case(snapshot: str, instruction: str, base_url: str) -> dict:
    """Draft a TestCase (name + steps) from a plain-English instruction and a
    one-time page snapshot. Returns a raw dict for :mod:`functional.generator`
    to validate into a TestCase — this is the compile-once planning call."""
    prompt = (
        "You are drafting an automated UI test case from an INSTRUCTION, exploring the "
        "page SNAPSHOT once. Produce ordered steps. Each interactive step MUST include a "
        "'hint' (natural-language description of the element) so it can self-heal. Use "
        "actions: goto, click, fill, select, wait, expect, verify_nl. For 'expect' set "
        "'assert' to one of visible|text|url|count. Only reference elements you can see "
        "in the snapshot.\n\n"
        f"{_GROUNDING}\n\n"
        f"BASE_URL: {base_url}\nINSTRUCTION: {instruction}\n\nSNAPSHOT:\n{snapshot}\n"
    )
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "target": {"type": "string"},
                        "selector": {"type": "string"},
                        "hint": {"type": "string"},
                        "value": {"type": "string"},
                        "assert": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["name", "steps"],
    }
    return _cached_invoke(
        "generate_case",
        {"instruction": instruction, "base_url": base_url, "snapshot": snapshot},
        prompt,
        schema,
    )
