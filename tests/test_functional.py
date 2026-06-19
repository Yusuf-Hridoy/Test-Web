"""Phase 2 functional engine — acceptance criteria 2-7.

The LLM touchpoint (``qascan.llm._invoke``) is patched with a FakeLLM so the full
stack (prompt build -> cache -> parse -> pydantic) runs without a Gemini key.
"""

from __future__ import annotations

from conftest import FakeLLM

from qascan import llm
from qascan.functional import assertions, auth
from qascan.functional.locators import HealCache, StepFailed, StepRef, resolve
from qascan.functional.schema import AuthConfig, Step, Suite


# --------------------------------------------------------------------------- #
# Criterion 2 + 7: hand-written YAML suite runs end-to-end, zero LLM on clean run
# --------------------------------------------------------------------------- #
def _deterministic_suite_yaml(base_url: str) -> str:
    return f"""
name: smoke
target:
  base_url: "{base_url}"
cases:
  - id: signin-flow
    name: Sign-in flow
    steps:
      - {{action: goto, target: "/form.html"}}
      - {{action: fill, selector: "#name", value: "Ada", hint: "the name field"}}
      - {{action: click, selector: "#go", hint: "the continue button"}}
      - {{action: wait}}
      - {{action: expect, assert: url, value: "welcome"}}
      - {{action: expect, assert: visible, selector: "#title"}}
      - {{action: expect, assert: text, selector: "#title", value: "Welcome aboard"}}
      - {{action: expect, assert: count, selector: ".item", value: "2"}}
"""


async def test_handwritten_suite_runs_clean_with_zero_llm(
    http_server, tmp_path, monkeypatch, isolated_cache
):
    from qascan.functional.executor import run_suite

    # Any LLM call on a clean run is a bug -> blow up loudly.
    def _boom(prompt, schema):
        raise AssertionError("LLM must not be called on a clean deterministic run")

    monkeypatch.setattr(llm, "_invoke", _boom)

    suite_file = tmp_path / "suite.yaml"
    suite_file.write_text(_deterministic_suite_yaml(http_server))
    suite = Suite.from_file(suite_file)

    result, out_dir = await run_suite(suite, out_root=tmp_path / "out")

    assert result.status == "pass"
    assert result.cases[0].status == "pass"
    assert result.llm_calls == 0  # criterion 7
    assert (out_dir / "results.json").exists()
    assert (out_dir / "report.html").exists()
    assert (out_dir / "trace.zip").exists()  # trace captured


# --------------------------------------------------------------------------- #
# Criterion 3: self-healing heals, caches, and re-run makes no new LLM call
# --------------------------------------------------------------------------- #
async def test_self_healing_heals_caches_and_reuses(page, http_server, tmp_path, monkeypatch):
    await page.goto(f"{http_server}/form.html", wait_until="domcontentloaded")

    # FakeLLM proposes the correct selector for the broken one.
    fake = FakeLLM(lambda p, s: {"selector": "#go", "confidence": 0.95, "provenance": "DERIVED"})
    monkeypatch.setattr(llm, "_invoke", fake)

    step = Step(action="click", selector="#go-wrong", hint="the continue button")
    ref = StepRef(suite_id="s", case_id="c", step_index=2)
    cache1 = HealCache("s", root=tmp_path)

    res1 = await resolve(page, step, ref, cache1)
    assert res1.healed is True
    assert len(fake.calls) == 1  # one heal call
    healed = cache1.healed_steps()
    assert healed and healed[0]["reviewed"] is False  # flagged for review

    # Re-run: a freshly-loaded cache (from disk) must NOT call the LLM again.
    fake.calls.clear()
    cache2 = HealCache("s", root=tmp_path)
    res2 = await resolve(page, step, ref, cache2)
    assert res2.healed is True
    assert len(fake.calls) == 0  # cache hit -> zero new LLM calls


async def test_low_confidence_heal_is_rejected(page, http_server, tmp_path, monkeypatch):
    await page.goto(f"{http_server}/form.html", wait_until="domcontentloaded")
    fake = FakeLLM(lambda p, s: {"selector": "#go", "confidence": 0.10})
    monkeypatch.setattr(llm, "_invoke", fake)

    step = Step(action="click", selector="#nope", hint="the continue button")
    ref = StepRef(suite_id="s2", case_id="c", step_index=0)
    try:
        await resolve(page, step, ref, HealCache("s2", root=tmp_path), threshold=0.7)
        raise AssertionError("expected StepFailed for low-confidence heal")
    except StepFailed as exc:
        assert "confidence" in str(exc).lower()
        assert exc.evidence  # carries snapshot/screenshot evidence


# --------------------------------------------------------------------------- #
# Criterion 4: deterministic assertions (hard pass/fail) + verify_nl (needs review)
# --------------------------------------------------------------------------- #
async def test_deterministic_assertions(page, http_server):
    await page.goto(f"{http_server}/welcome.html", wait_until="domcontentloaded")

    visible = await assertions.evaluate(
        page, Step(action="expect", assert_="visible", selector="#title")
    )
    assert visible.deterministic and visible.passed is True and not visible.needs_review

    bad_text = await assertions.evaluate(
        page, Step(action="expect", assert_="text", selector="#title", value="nope")
    )
    assert bad_text.passed is False

    count_ok = await assertions.evaluate(
        page, Step(action="expect", assert_="count", selector=".item", value="2")
    )
    assert count_ok.passed is True


async def test_verify_nl_is_needs_review_with_confidence(page, http_server, monkeypatch):
    await page.goto(f"{http_server}/welcome.html", wait_until="domcontentloaded")
    fake = FakeLLM(lambda p, s: {"passed": True, "reason": "heading reads 'Welcome aboard'",
                                 "confidence": 0.9, "provenance": "DERIVED"})
    monkeypatch.setattr(llm, "_invoke", fake)

    out = await assertions.evaluate(
        page, Step(action="verify_nl", value="the page welcomes the user")
    )
    assert out.kind == "verify_nl"
    assert out.deterministic is False
    assert out.needs_review is True  # criterion 4: shown as needs review
    assert out.confidence == 0.9
    assert out.passed is True


async def test_verify_nl_unsure_is_flagged_verify(page, http_server, monkeypatch):
    await page.goto(f"{http_server}/welcome.html", wait_until="domcontentloaded")
    fake = FakeLLM(lambda p, s: {"passed": None, "reason": "cannot tell", "confidence": 0.2})
    monkeypatch.setattr(llm, "_invoke", fake)
    out = await assertions.evaluate(page, Step(action="verify_nl", value="is the user an admin?"))
    assert out.passed is None and out.needs_review is True
    assert out.provenance == llm.VERIFY


# --------------------------------------------------------------------------- #
# Criterion 5: auth — context kwargs, and fail-fast on expired session
# --------------------------------------------------------------------------- #
def test_build_context_kwargs():
    tok = auth.build_context_kwargs(AuthConfig(kind="token", token="abc"))
    assert tok["extra_http_headers"]["Authorization"] == "Bearer abc"

    state = auth.build_context_kwargs(
        AuthConfig(kind="storage_state", storage_state={"cookies": [], "origins": []})
    )
    assert state["storage_state"] == {"cookies": [], "origins": []}


async def test_expired_session_fails_fast(http_server, tmp_path, monkeypatch, isolated_cache):
    from qascan.functional.executor import run_suite

    monkeypatch.setattr(llm, "_invoke", lambda p, s: (_ for _ in ()).throw(AssertionError()))

    suite = Suite.model_validate({
        "name": "authed",
        "target": {
            "base_url": http_server,
            "auth": {
                "kind": "storage_state",
                "verify_url": f"{http_server}/login.html",
                "login_redirect_marker": "login.html",
            },
        },
        "cases": [
            {"id": "c1", "name": "c1", "steps": [{"action": "goto", "target": "/form.html"}]}
        ],
    })

    result, out_dir = await run_suite(suite, out_root=tmp_path / "out")
    assert result.status == "session_expired"
    assert result.cases == []  # no flood of false failures
    assert "re-capture" in result.message.lower()
    assert (out_dir / "report.html").exists()


# --------------------------------------------------------------------------- #
# Criterion 6: generate writes editable YAML; running it uses zero planning calls
# --------------------------------------------------------------------------- #
async def test_generate_then_run_uses_zero_planning_calls(
    http_server, tmp_path, monkeypatch, isolated_cache
):
    from qascan.functional.executor import run_suite
    from qascan.functional.generator import generate

    drafted = {
        "name": "generated signin",
        "steps": [
            {"action": "goto", "target": "/form.html"},
            {"action": "fill", "selector": "#name", "value": "Ada", "hint": "name field"},
            {"action": "click", "selector": "#go", "hint": "continue button"},
            {"action": "wait"},
            {"action": "expect", "assert": "url", "value": "welcome"},
        ],
    }
    fake = FakeLLM(lambda p, s: drafted)
    monkeypatch.setattr(llm, "_invoke", fake)

    out_yaml = tmp_path / "generated.yaml"
    suite, path = await generate(f"{http_server}/form.html", "sign in as Ada", out_yaml)
    assert path.exists()
    assert suite.cases[0].source == "generated"
    assert len(fake.calls) == 1  # one planning call

    # Running the saved case must NOT re-plan -> zero LLM calls.
    fake.calls.clear()
    loaded = Suite.from_file(out_yaml)
    result, _ = await run_suite(loaded, out_root=tmp_path / "out")
    assert result.status == "pass"
    assert len(fake.calls) == 0  # criterion 6: zero planning LLM calls on run
    assert result.llm_calls == 0


def test_schema_yaml_roundtrip_assert_alias():
    s = Step.model_validate({"action": "expect", "assert": "visible", "selector": "#x"})
    assert s.assert_ == "visible"
    dumped = s.model_dump(by_alias=True, exclude_none=True)
    assert dumped["assert"] == "visible"
