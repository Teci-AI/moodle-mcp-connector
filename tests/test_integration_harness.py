"""Safety rules of the live test harness in tests/integration, without a live site.

The live tests must skip unless configured, never delete a course they did
not create, and never show the token.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "tests" / "integration"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


harness = load("integration_conftest", INTEGRATION / "conftest.py")
live = load("integration_test_live", INTEGRATION / "test_live.py")


# --- Opt-in only ------------------------------------------------------------ #

@pytest.mark.skipif((ROOT / ".env.test").exists(),
                    reason=".env.test is present, so the live tests would run")
def test_live_tests_skip_without_settings():
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOODLE_TEST_")}
    run = subprocess.run([sys.executable, "-m", "pytest", "tests/integration", "-q",
                          "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:]
    summary = run.stdout.strip().splitlines()[-1]
    assert "skipped" in summary and "passed" not in summary and "failed" not in summary


def test_settings_come_from_env_file_and_env_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.test"
    env_file.write_text("MOODLE_TEST_URL=https://file.example/\nMOODLE_TEST_TOKEN=file-token\n"
                        "MOODLE_TEST_CATEGORYID=12\n")
    monkeypatch.setattr(harness, "ENV_FILE", env_file)
    for name in ("MOODLE_TEST_URL", "MOODLE_TEST_TOKEN", "MOODLE_TEST_CATEGORYID"):
        monkeypatch.delenv(name, raising=False)
    settings = harness._load_settings()
    assert (settings.url, settings.token, settings.categoryid) == (
        "https://file.example", "file-token", 12)

    monkeypatch.setenv("MOODLE_TEST_TOKEN", "env-token")
    assert harness._load_settings().token == "env-token"


def test_settings_missing_means_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "ENV_FILE", tmp_path / "absent")
    monkeypatch.delenv("MOODLE_TEST_URL", raising=False)
    monkeypatch.setenv("MOODLE_TEST_TOKEN", "t")
    assert harness._load_settings() is None


# --- The documented service has exactly the functions the live tests use ----- #

def test_contributing_lists_exactly_the_functions_the_live_tests_need():
    import re

    from moodle_mcp_connector import server
    from test_readme import _functions_reached

    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    block = contributing.split("## Live integration tests", 1)[1].split("```", 2)[1]
    documented = set(block.split())

    source = (INTEGRATION / "test_live.py").read_text(encoding="utf-8")
    tools = set(re.findall(r'call\(\s*"(\w+)"', source))
    tools |= set(re.findall(r'\("(create_\w+)", "\w+", \{', source))  # parametrized
    core = {f for f in _functions_reached(tools) if not f.startswith(server.PLUGIN_PREFIX)}
    server_source = Path(server.__file__).read_text(encoding="utf-8")
    plugin = set(re.findall(r'"(local_mcpconnector_\w+)"', server_source)) | {
        server.PLUGIN_PREFIX + n for n in re.findall(r'PLUGIN_PREFIX \+ "(\w+)"', server_source)}

    assert documented == core | plugin, documented ^ (core | plugin)


# --- The token is never shown ------------------------------------------------ #

def test_settings_repr_hides_the_token():
    settings = harness.LiveSettings("https://m.example", "super-secret", 3)
    assert "super-secret" not in repr(settings)
    assert "***" in repr(settings)


def test_reports_are_scrubbed_of_the_token(monkeypatch):
    monkeypatch.setattr(harness, "SETTINGS", harness.LiveSettings("https://m", "tok-123", None))
    report = SimpleNamespace(longrepr="E  Moodle said tok-123 is bad",
                             sections=[("Captured stdout call", "token=tok-123 here")])
    wrapper = harness.pytest_runtest_makereport(item=None, call=None)
    next(wrapper)
    with pytest.raises(StopIteration) as done:
        wrapper.send(report)
    scrubbed = done.value.value
    assert "tok-123" not in scrubbed.longrepr and "***" in scrubbed.longrepr
    assert scrubbed.sections == [("Captured stdout call", "token=*** here")]


# --- Cleanup deletes only this run's mcptest- courses ------------------------ #

class FakeSite:
    def __init__(self, courses: dict[int, str]):
        self.courses = dict(courses)  # id -> shortname
        self.deleted: list[list[int]] = []

    def find_courses(self, field, value):
        return [{"id": cid, "shortname": sn} for cid, sn in self.courses.items()
                if (field == "id" and str(cid) == value) or (field == "shortname" and sn == value)]

    def call(self, tool, /, **args):
        assert tool == "delete_courses"
        self.deleted.append(args["courseids"])
        for cid in args["courseids"]:
            self.courses.pop(cid, None)
        return {"warnings": []}


@pytest.fixture
def site(monkeypatch):
    def install(courses):
        fake = FakeSite(courses)
        monkeypatch.setattr(live, "find_courses", fake.find_courses)
        monkeypatch.setattr(live, "call", fake.call)
        return fake
    return install


def test_cleanup_deletes_registered_courses_by_shortname_and_id(site):
    registry = live.Courses()
    main, built = registry.shortname("main"), registry.shortname("build")
    fake = site({10: main, 11: built, 12: registry.shortname("restored") + "_1",
                 99: "real-course"})
    registry.track(12)
    assert registry.cleanup() == []
    assert sorted(fake.deleted[0]) == [10, 11, 12]
    assert fake.courses == {99: "real-course"}


def test_cleanup_never_deletes_a_course_that_is_not_a_test_course(site):
    registry = live.Courses()
    fake = site({42: "chemistry-101"})
    registry.track(42)  # e.g. a restore that somehow reported a real course id
    problems = registry.cleanup()
    assert fake.deleted == []
    assert fake.courses == {42: "chemistry-101"}
    assert problems == ["refusing to delete course 42 ('chemistry-101'): not a test course"]


def test_cleanup_reports_courses_that_survive(site, monkeypatch):
    registry = live.Courses()
    fake = site({7: registry.shortname("main")})
    monkeypatch.setattr(live, "call", lambda tool, /, **args: {"warnings": []})  # deletes nothing
    problems = registry.cleanup()
    assert problems and "still exists" in problems[0]
    assert 7 in fake.courses


def test_cleanup_with_nothing_created_is_quiet(site):
    registry = live.Courses()
    registry.shortname("rollback-fail")  # planned but never created
    fake = site({1: "other"})
    assert registry.cleanup() == []
    assert fake.deleted == []


def test_shortnames_are_unique_and_prefixed():
    a, b = live.Courses(), live.Courses()
    assert a.shortname("main").startswith("mcptest-")
    assert a.stamp != b.stamp or a.shortname("x") != b.shortname("y")
