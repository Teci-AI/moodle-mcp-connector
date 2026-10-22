"""Fakes for the unit tests: a stand-in for server._call and a fake REST endpoint.

Tests import helpers from here (not from conftest): tests/integration has a
conftest.py of its own, and two modules named "conftest" would shadow each
other.
"""

from urllib.parse import parse_qs

import httpx


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


class FakeMoodle:
    """A fake Moodle REST endpoint behind the real server._call.

    `responses` maps wsfunction to a JSON payload or to a callable taking the
    decoded form fields. GET requests (file downloads) go to `files`, keyed
    by URL path. Every request is recorded in `requests`.
    """

    def __init__(self, responses: dict, files: dict | None = None):
        self.responses = responses
        self.files = files or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            body = self.files.get(request.url.path)
            if body is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, content=body)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        fn = form["wsfunction"]
        if fn not in self.responses:
            raise AssertionError(f"unexpected Moodle call: {fn}")
        response = self.responses[fn]
        payload = response(form) if callable(response) else response
        return httpx.Response(200, json=payload)

    def called(self, wsfunction: str) -> bool:
        return any(
            r.method == "POST" and f"wsfunction={wsfunction}" in r.content.decode()
            for r in self.requests
        )


def moodle_error(errorcode: str, message: str = "error") -> dict:
    """A Moodle REST error payload."""
    return {"exception": "moodle_exception", "errorcode": errorcode, "message": message}


def site_info(*function_names: str) -> dict:
    """A core_webservice_get_site_info payload listing the given functions."""
    return {
        "sitename": "Test site",
        "release": "5.3",
        "userid": 2,
        "functions": [{"name": n} for n in ("core_webservice_get_site_info", *function_names)],
    }
