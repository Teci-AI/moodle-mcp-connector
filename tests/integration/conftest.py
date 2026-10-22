"""Live integration tests against a real Moodle site (opt-in).

They run only when MOODLE_TEST_URL and MOODLE_TEST_TOKEN are set, in the
environment or in a git-ignored .env.test at the repository root (the
environment wins). MOODLE_TEST_CATEGORYID names the category the throwaway
test courses are created in; tests that need a course skip without it.
CI never sets these, so CI always skips this directory.

The token is never printed: it is not put in any assertion message, and
every report is scrubbed of it before pytest shows it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = ROOT / ".env.test"


@dataclass(frozen=True)
class LiveSettings:
    url: str
    token: str
    categoryid: int | None

    def __repr__(self) -> str:  # never show the token, even in --showlocals
        return f"LiveSettings(url={self.url!r}, token='***', categoryid={self.categoryid})"


def _load_settings() -> LiveSettings | None:
    values = dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}

    def get(name: str) -> str:
        return (os.environ.get(name) or values.get(name) or "").strip()

    url, token = get("MOODLE_TEST_URL").rstrip("/"), get("MOODLE_TEST_TOKEN")
    if not url or not token:
        return None
    category = get("MOODLE_TEST_CATEGORYID")
    return LiveSettings(url, token, int(category) if category else None)


SETTINGS = _load_settings()
SKIP_REASON = ("live Moodle tests: set MOODLE_TEST_URL and MOODLE_TEST_TOKEN "
               "(environment or .env.test) to run them")


def pytest_collection_modifyitems(config, items):
    if SETTINGS is not None:
        return
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.skip(reason=SKIP_REASON))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Scrub the token from failure reports and captured output."""
    report = yield
    if SETTINGS is not None:
        token = SETTINGS.token
        if report.longrepr is not None and token in str(report.longrepr):
            report.longrepr = str(report.longrepr).replace(token, "***")
        report.sections = [(title, text.replace(token, "***"))
                           for title, text in report.sections]
    return report


@pytest.fixture(scope="module")
def live() -> LiveSettings:
    """Point the server at the live site for one test module, then restore it."""
    if SETTINGS is None:
        pytest.skip(SKIP_REASON)
    from moodle_mcp_connector import server

    if not server.ALLOW_WRITE:
        pytest.fail("write tools are not registered; tests/conftest.py must enable them")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "MOODLE_URL", SETTINGS.url)
        mp.setattr(server, "MOODLE_TOKEN", SETTINGS.token)
        mp.setattr(server, "REST_ENDPOINT", f"{SETTINGS.url}/webservice/rest/server.php")
        mp.setattr(server, "_USERID_CACHE", {})
        yield SETTINGS


@pytest.fixture(scope="module")
def categoryid(live: LiveSettings) -> int:
    if live.categoryid is None:
        pytest.skip("set MOODLE_TEST_CATEGORYID to create the throwaway test courses")
    return live.categoryid
