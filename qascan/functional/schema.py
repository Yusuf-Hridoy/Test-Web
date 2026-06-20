"""Declarative test-case contract (pydantic). Suites load from YAML or JSON.

``hint`` is what makes self-healing possible — every interactive step should
carry a natural-language description of its target element.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Action = Literal["goto", "click", "fill", "select", "wait", "expect", "verify_nl"]
AssertKind = Literal["visible", "text", "url", "count"]


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action: Action
    target: str | None = None  # url for goto
    selector: str | None = None  # deterministic locator
    hint: str | None = None  # NL description for self-healing
    value: str | None = None  # text to fill / expected value
    # 'assert' is a Python keyword, so the field is assert_ with alias 'assert'.
    assert_: AssertKind | None = Field(default=None, alias="assert")


class AuthConfig(BaseModel):
    """How to authenticate before running a suite. storage_state is the default."""

    kind: Literal["storage_state", "token", "form_login"] = "storage_state"
    # storage_state: a path to a Playwright storage-state file, or inline JSON.
    storage_state: str | dict | None = None
    # token: header-based / bearer injection.
    token: str | None = None
    token_header: str = "Authorization"
    token_scheme: str = "Bearer"
    # form_login: fallback only (no MFA/CAPTCHA automation, ever).
    login_url: str | None = None
    username: str | None = None
    password: str | None = None
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    # URL hit before a run to confirm the session is still valid.
    verify_url: str | None = None
    # A substring whose presence in the post-load URL indicates a login redirect
    # (i.e. an expired session).
    login_redirect_marker: str | None = None


class TargetConfig(BaseModel):
    base_url: str
    auth: AuthConfig | None = None


class TestCase(BaseModel):
    __test__ = False  # not a pytest test class (name starts with "Test")

    id: str
    name: str
    steps: list[Step]
    source: Literal["written", "generated"] = "written"  # provenance


class Suite(BaseModel):
    name: str
    target: TargetConfig
    cases: list[TestCase]

    @classmethod
    def from_file(cls, path: str | Path) -> Suite:
        """Load a suite from a YAML or JSON file."""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False
        )
