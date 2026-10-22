"""Packaging metadata: one version, sensible bounds, nothing built gets tracked."""

import importlib.metadata as metadata
import subprocess
import tomllib
from pathlib import Path

import pytest

import moodle_mcp_connector
from moodle_mcp_connector import server

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]
REPO = "https://github.com/Teci-AI/moodle-mcp-connector"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_version_is_the_same_everywhere():
    installed = metadata.version("moodle-mcp-connector")
    assert installed == PROJECT["version"], (
        "installed metadata is stale; reinstall with: pip install -e \".[dev]\"")
    assert server.SERVER_VERSION == PROJECT["version"]
    assert moodle_mcp_connector.__version__ == PROJECT["version"]


def test_version_is_written_only_in_pyproject():
    version = PROJECT["version"]
    for path in (ROOT / "moodle_mcp_connector").glob("*.py"):
        assert f'"{version}"' not in path.read_text(encoding="utf-8"), path.name


def test_uninstalled_checkout_reports_unknown_version(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(server._metadata, "version", missing)
    assert server._package_version() == "0+unknown"


def test_installed_dependencies_satisfy_declared_bounds():
    requirements = pytest.importorskip("packaging.requirements")
    for spec in PROJECT["dependencies"]:
        req = requirements.Requirement(spec)
        assert req.specifier, f"{req.name} has no lower bound"
        assert any(s.operator == ">=" for s in req.specifier), f"{req.name} has no >= bound"
        installed = metadata.version(req.name)
        assert req.specifier.contains(installed, prereleases=True), (req.name, installed)


def test_project_metadata():
    assert PROJECT["license"] == "MIT"
    assert PROJECT["authors"] == [{"name": "Teci"}]
    assert PROJECT["urls"] == {
        "Homepage": REPO,
        "Repository": REPO,
        "Issues": f"{REPO}/issues",
        "Changelog": f"{REPO}/blob/main/CHANGES.md",
    }
    classifiers = PROJECT["classifiers"]
    assert "Development Status :: 3 - Alpha" in classifiers
    assert "Topic :: Education" in classifiers
    # setuptools>=77 refuses License classifiers next to an SPDX license.
    assert not [c for c in classifiers if c.startswith("License ::")]


def test_python_classifiers_match_requires_python():
    versions = sorted(
        (int(c.rsplit(".", 1)[1]) for c in PROJECT["classifiers"]
         if c.startswith("Programming Language :: Python :: 3.")),
    )
    assert versions == [11, 12, 13, 14]
    assert PROJECT["requires-python"] == f">=3.{versions[0]}"


@pytest.mark.parametrize("path", [
    "moodle_mcp_connector.egg-info/PKG-INFO",
    "build/lib/moodle_mcp_connector/server.py",
    "dist/moodle_mcp_connector-0.1.0-py3-none-any.whl",
    "dist/moodle_mcp_connector-0.1.0.tar.gz",
])
def test_build_artifacts_are_ignored(path):
    assert git("check-ignore", "-q", "--no-index", path).returncode == 0, path


def test_no_build_artifacts_are_tracked():
    tracked = git("ls-files").stdout.splitlines()
    assert not [p for p in tracked
                if ".egg-info" in p or p.startswith(("build/", "dist/"))]
