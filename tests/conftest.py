"""Shared test setup.

server.py reads its configuration at import time, so the environment is set
here, before any test imports it. Write tools are enabled so every tool is
registered; no unit test talks to a real Moodle site (the opt-in live tests
in tests/integration point the server elsewhere themselves). The fakes the
fixtures install live in fakes.py.
"""

import os

os.environ.update(
    MOODLE_URL="https://moodle.test",
    MOODLE_TOKEN="SECRET-TEST-TOKEN",
    MOODLE_ALLOW_WRITE="true",
    MOODLE_TOOLSET="",
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from fakes import FakeCall, FakeMoodle  # noqa: E402
from moodle_mcp_connector import server  # noqa: E402


@pytest.fixture
def fake_call(monkeypatch):
    """Return a factory: fake_call({wsfunction: response}) installs a FakeCall."""

    def install(responses: dict) -> FakeCall:
        fake = FakeCall(responses)
        monkeypatch.setattr(server, "_call", fake)
        return fake

    return install


@pytest.fixture
def fake_http(monkeypatch):
    """Return a factory: fake_http(responses, files) routes httpx to a FakeMoodle."""
    real_client = httpx.AsyncClient

    def install(responses: dict, files: dict | None = None) -> FakeMoodle:
        fake = FakeMoodle(responses, files)
        transport = httpx.MockTransport(fake.handler)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
        )
        return fake

    return install
