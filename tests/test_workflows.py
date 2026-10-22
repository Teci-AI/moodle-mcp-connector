"""GitHub Actions workflows: triggers, action versions, and the CI matrix.

Plain text checks, so the tests need no YAML library. The workflows were
also linted with actionlint when they were written.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
CLASSIFIER_VERSIONS = sorted(
    c.rsplit(":: ", 1)[1] for c in PROJECT["classifiers"]
    if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
)
# Minimum major versions: older releases run on Node.js 20.
MIN_ACTION_MAJOR = {"actions/checkout": 5, "actions/setup-python": 6}


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def job(text: str, name: str) -> str:
    """The text of one job (from its key to the next job at the same indent)."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match, f"job {name} not found"
    return match.group(1)


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_only_current_actions_are_used(path):
    uses = re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@v(\d+)", path.read_text(encoding="utf-8"))
    assert uses, "no actions found"
    for action, major in uses:
        assert action in MIN_ACTION_MAJOR, f"unexpected action {action}"
        assert int(major) >= MIN_ACTION_MAJOR[action], f"{action}@v{major} is too old"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_workflow_gets_live_moodle_settings(path):
    # Comments may mention the variables; no env, secret or step may set them.
    code = [line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()]
    assert not [line for line in code if "MOODLE_TEST_" in line]


def test_ci_runs_on_branch_pushes_and_pull_requests_not_tags():
    trigger = CI.split("\non:", 1)[1].split("\npermissions:", 1)[0]
    assert re.search(r"push:\n\s+branches: \[\"\*\*\"\]", trigger)
    assert "pull_request:" in trigger
    assert "tags" not in trigger


def test_ci_matrix_matches_python_classifiers():
    matrix = re.search(r"python-version: \[(.*?)\]", job(CI, "test")).group(1)
    assert sorted(v.strip().strip('"') for v in matrix.split(",")) == CLASSIFIER_VERSIONS
    assert "runs-on: ubuntu-latest" in job(CI, "test")


def test_windows_job_uses_newest_python():
    windows = job(CI, "test-windows")
    assert "runs-on: windows-latest" in windows
    assert f'python-version: "{CLASSIFIER_VERSIONS[-1]}"' in windows
    assert "python -m pytest" in windows


def test_lowest_dependencies_job_uses_oldest_python_and_floors():
    lowest = job(CI, "lowest-dependencies")
    assert f'python-version: "{CLASSIFIER_VERSIONS[0]}"' in lowest
    assert "--resolution lowest-direct" in lowest
    assert "python -m pytest" in lowest


def test_build_job_builds_checks_and_smoke_tests_once():
    build = job(CI, "build")
    for step in ("python -m build", "twine check --strict dist/*",
                 "python scripts/check_wheel.py dist"):
        assert step in build
    assert "matrix" not in build
