"""Authentication via storage-state (default), token injection, or form login.

The human does the hard login (MFA/CAPTCHA/SSO) in a headed browser; we capture
the resulting session. We NEVER automate MFA/CAPTCHA. Staging-only by default.

Before a suite runs, the session is verified — an expired session fails fast with
a clear "re-capture" message instead of flooding the report with false failures.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from .schema import AuthConfig


def build_context_kwargs(auth: AuthConfig | None) -> dict:
    """Kwargs for ``browser.new_context(...)`` derived from the auth config."""
    if auth is None:
        return {}
    kwargs: dict = {}
    if auth.kind == "storage_state" and auth.storage_state is not None:
        state = auth.storage_state
        if isinstance(state, str):
            # A path on disk, or inline JSON text.
            p = Path(state)
            kwargs["storage_state"] = json.loads(state) if not p.exists() else str(p)
        else:
            kwargs["storage_state"] = state
    if auth.kind == "token" and auth.token:
        value = f"{auth.token_scheme} {auth.token}".strip()
        kwargs["extra_http_headers"] = {auth.token_header: value}
    return kwargs


async def perform_form_login(context: BrowserContext, auth: AuthConfig) -> None:
    """Fallback form login. No MFA/CAPTCHA handling — those need human capture."""
    if not (auth.login_url and auth.username_selector and auth.password_selector):
        raise ValueError("form_login requires login_url + username/password selectors.")
    page = await context.new_page()
    await page.goto(auth.login_url, wait_until="domcontentloaded")
    await page.fill(auth.username_selector, auth.username or "")
    await page.fill(auth.password_selector, auth.password or "")
    if auth.submit_selector:
        await page.click(auth.submit_selector)
    await page.wait_for_load_state("networkidle")
    await page.close()


class SessionExpired(Exception):
    """The captured session is no longer valid — re-capture is required."""


async def assert_session_valid(
    context: BrowserContext, auth: AuthConfig | None, base_url: str
) -> None:
    """Hit an authenticated URL; raise ``SessionExpired`` on a login redirect."""
    if auth is None:
        return
    target = auth.verify_url or base_url
    page = await context.new_page()
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=15_000)
        landed = page.url
    except PlaywrightError as exc:
        raise SessionExpired(
            f"Could not reach {target} to verify the session: {str(exc).splitlines()[0]}"
        ) from exc
    finally:
        await page.close()

    marker = auth.login_redirect_marker
    if marker and marker in landed:
        raise SessionExpired(
            f"Session looks expired: requested {target} but landed on {landed} "
            f"(matched login marker '{marker}'). Re-capture with `qascan auth capture`."
        )


async def capture_storage_state(url: str, out_path: str | Path) -> Path:
    """Open a HEADED browser, let the human log in, then save the storage state."""
    out_path = Path(out_path)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        print(
            "\nA browser window is open. Log in manually (MFA/CAPTCHA/SSO included).\n"
            "When you are fully logged in, return here and press Enter to capture."
        )
        await asyncio.get_event_loop().run_in_executor(None, input, "Press Enter to save… ")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(out_path))
        await context.close()
        await browser.close()
    return out_path
