"""Shared test setup.

server.py reads its configuration at import time, so the environment is set
here, before any test imports it. Write tools are enabled so every tool is
registered; no test talks to a real Moodle site.
"""

import os

os.environ.update(
    MOODLE_URL="https://moodle.test",
    MOODLE_TOKEN="SECRET-TEST-TOKEN",
    MOODLE_ALLOW_WRITE="true",
    MOODLE_TOOLSET="",
)

import pytest  # noqa: E402

from moodle_mcp_connector import server  # noqa: E402


class FakeCall:
    """Stand-in for server._call that answers from a table of responses.

    Each response is a value to return, an exception to raise, or a callable
    taking the params dict. Every call is recorded in `calls` as
    (wsfunction, params). Calling an unlisted function fails the test.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, wsfunction: str, params: dict | None = None):
        self.calls.append((wsfunction, params or {}))
        if wsfunction not in self.responses:
            raise AssertionError(f"unexpected Moodle call: {wsfunction}")
        response = self.responses[wsfunction]
        if isinstance(response, BaseException):
            raise response
        return response(params or {}) if callable(response) else response

    def called(self, wsfunction: str) -> bool:
        return any(name == wsfunction for name, _ in self.calls)


@pytest.fixture
def fake_call(monkeypatch):
    """Return a factory: fake_call({wsfunction: response}) installs a FakeCall."""

    def install(responses: dict) -> FakeCall:
        fake = FakeCall(responses)
        monkeypatch.setattr(server, "_call", fake)
        return fake

    return install


def site_info(*function_names: str) -> dict:
    """A core_webservice_get_site_info payload listing the given functions."""
    return {
        "sitename": "Test site",
        "release": "5.3",
        "userid": 2,
        "functions": [{"name": n} for n in ("core_webservice_get_site_info", *function_names)],
    }
