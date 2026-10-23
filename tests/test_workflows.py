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
# Minimum major versions: older releases run on Node.js 20. (Checked when
# adopted: these majors use node24; the PyPI action is a Docker-based
# composite action on PyPA's maintained release/v1 branch.)
MIN_ACTION_MAJOR = {"actions/checkout": 5, "actions/setup-python": 6,
                    "actions/upload-artifact": 7, "actions/download-artifact": 8}
PINNED_REFS = {"pypa/gh-action-pypi-publish": "release/v1"}


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def job(text: str, name: str) -> str:
    """The text of one job (from its key to the next job at the same indent)."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match, f"job {name} not found"
    return match.group(1)


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_only_current_actions_are_used(path):
    uses = re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)", path.read_text(encoding="utf-8"))
    assert uses, "no actions found"
    for action, ref in uses:
        if action in PINNED_REFS:
            assert ref == PINNED_REFS[action], f"{action}@{ref}"
            continue
        assert action in MIN_ACTION_MAJOR, f"unexpected action {action}"
        major = re.fullmatch(r"v(\d+)", ref)
        assert major, f"{action}@{ref} should use a major version tag"
        assert int(major.group(1)) >= MIN_ACTION_MAJOR[action], f"{action}@{ref} is too old"


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


RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


RELEASE_JOBS = {name: job(RELEASE, name)
                for name in ("build", "publish-testpypi", "publish-pypi", "github-release")}
ON_TAG = "if: github.event_name == 'push' && github.ref_type == 'tag'"
ON_MANUAL = "if: github.event_name == 'workflow_dispatch'"


def test_release_runs_on_version_tags_and_by_hand_only():
    trigger = RELEASE.split("\non:", 1)[1].split("\npermissions:", 1)[0]
    assert re.search(r'push:\n\s+tags: \["v\*"\]', trigger)
    assert "workflow_dispatch:" in trigger
    assert "branches" not in trigger and "pull_request" not in trigger


def test_release_permissions_are_least_privilege():
    assert re.search(r"^permissions:\n  contents: read\n", RELEASE, re.M)
    # Only the publishing jobs may request PyPI's OIDC token...
    for name, text in RELEASE_JOBS.items():
        has_oidc = "id-token: write" in text
        assert has_oidc == name.startswith("publish-"), name
    # ...and only the GitHub release job may write to the repository.
    for name, text in RELEASE_JOBS.items():
        assert ("contents: write" in text) == (name == "github-release"), name
    assert "GH_TOKEN: ${{ github.token }}" in RELEASE_JOBS["github-release"]


def test_release_build_checks_tests_and_builds_in_order():
    build = RELEASE_JOBS["build"]
    steps = ["scripts/release_check.py --tag \"$TAG\"", "python -m pytest", "python -m build",
             "twine check --strict dist/*", "scripts/check_wheel.py dist",
             "actions/upload-artifact"]
    positions = [build.index(step) for step in steps]
    assert positions == sorted(positions), "steps are out of order"
    # The tag is only checked for pushed tags; manual runs check version and notes.
    assert "TAG: ${{ github.event_name == 'push' && github.ref_name || '' }}" in build
    assert "python scripts/release_check.py --notes-out release-notes.md" in build


def test_pypi_publishing_only_on_tags_after_build_with_approval_environment():
    pypi = RELEASE_JOBS["publish-pypi"]
    assert ON_TAG in pypi and "needs: build" in pypi
    assert "name: pypi\n" in pypi
    assert "uses: pypa/gh-action-pypi-publish@release/v1" in pypi
    assert "repository-url" not in pypi  # the real PyPI


def test_testpypi_publishing_only_on_manual_runs():
    test = RELEASE_JOBS["publish-testpypi"]
    assert ON_MANUAL in test and "needs: build" in test
    assert "name: testpypi\n" in test
    assert "repository-url: https://test.pypi.org/legacy/" in test
    assert "skip-existing: true" in test


def test_publishing_uses_trusted_publishing_not_tokens():
    for forbidden in ("password:", "secrets.", "twine upload", "PYPI_API_TOKEN"):
        assert forbidden not in RELEASE


def test_github_release_comes_after_pypi_with_both_files():
    release = RELEASE_JOBS["github-release"]
    assert ON_TAG in release
    assert "needs: [build, publish-pypi]" in release
    assert 'gh release create "$TAG" dist/*' in release
    assert "--notes-file release-notes.md" in release
    assert "--verify-tag" in release
    assert "PRERELEASE: ${{ needs.build.outputs.prerelease }}" in release
    assert 'if [ "$PRERELEASE" = "true" ]; then\n            flags+=(--prerelease)' in release


def test_default_branch_is_main_everywhere():
    """Links and triggers use main (the plugin repository's default), never master."""
    import subprocess

    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.split()
    links = []
    for name in files:
        path = ROOT / name
        if path.suffix not in {".md", ".toml", ".yml", ".py"} or path == Path(__file__):
            continue
        text = path.read_text(encoding="utf-8")
        assert "master" not in text.lower(), f"{name} mentions master"
        links += re.findall(r"github\.com/Teci-AI/[\w.-]+/(?:blob|tree)/([\w.-]+)/", text)
    assert links and set(links) == {"main"}
    # CI must not be pinned to a branch name that a rename would break.
    assert 'branches: ["**"]' in CI


def test_build_job_builds_checks_and_smoke_tests_once():
    build = job(CI, "build")
    for step in ("python -m build", "twine check --strict dist/*",
                 "python scripts/check_wheel.py dist"):
        assert step in build
    assert "matrix" not in build
