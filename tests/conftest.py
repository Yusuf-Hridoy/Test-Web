"""Shared pytest fixtures: a local HTTP server serving tests/fixtures/, and a
Playwright page. The server lets us exercise real HTTP statuses (404/500) and
link liveness without touching the network."""

from __future__ import annotations

import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

FIXTURES = Path(__file__).parent / "fixtures"


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def _maybe_boom(self) -> bool:
        # Any path containing "boom" returns a 500 to model a server error.
        if "boom" in self.path:
            self.send_error(500, "Intentional server error")
            return True
        return False

    def do_GET(self):  # noqa: N802
        if self._maybe_boom():
            return
        super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if self._maybe_boom():
            return
        super().do_HEAD()

    def log_message(self, *args):  # silence server logging in tests
        pass


@pytest.fixture(scope="session")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        pg = await context.new_page()
        try:
            yield pg
        finally:
            await context.close()
            await browser.close()


class FakeLLM:
    """Records calls and returns canned JSON, standing in for Gemini's wire layer.

    Install by patching ``qascan.llm._invoke`` (the single touchpoint), so the whole
    prompt/cache/parse stack runs unchanged with no API key.
    """

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[str] = []

    def __call__(self, prompt: str, schema: dict) -> dict:
        self.calls.append(prompt)
        return self.responder(prompt, schema)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point all qascan caches at a fresh tmp dir per test — no repo pollution,
    no cross-test cache contamination."""
    monkeypatch.setenv("QASCAN_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture(scope="session")
def db_engine():
    """Engine bound to the test database (DATABASE_URL_TEST). Skips if unset."""
    import os

    from dotenv import load_dotenv

    from qascan.db.session import get_engine

    load_dotenv()
    url = os.getenv("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST not set — skipping DB tests.")
    engine = get_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Fresh schema per test (drop + create), yielding a session."""
    from sqlalchemy.orm import sessionmaker

    from qascan.db.models import Base

    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
