"""scripts/release_check.py: tag/version match, release notes, pre-release flag."""

import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_check.py"
spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
CHANGES = """# Changes

## 0.2.0 (2026-11-01)

- Newer things.

## 0.1.0 (2026-10-22)

First release.

- A tool.
"""


def test_matching_tag_passes():
    rc.check_tag("v0.1.0", "0.1.0")


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1.1", "v0.1", "v0.1.0-rc1", "release-0.1.0"])
def test_mismatched_tag_fails_clearly(tag):
    with pytest.raises(rc.ReleaseError) as exc:
        rc.check_tag(tag, "0.1.0")
    assert "does not match the version in pyproject.toml (0.1.0)" in str(exc.value)
    assert "'v0.1.0'" in str(exc.value)


def test_notes_are_exactly_the_matching_section():
    assert rc.release_notes(CHANGES, "0.1.0") == "First release.\n\n- A tool.\n"
    assert rc.release_notes(CHANGES, "0.2.0") == "- Newer things.\n"


def test_missing_section_fails_and_lists_what_exists():
    with pytest.raises(rc.ReleaseError) as exc:
        rc.release_notes(CHANGES, "0.3.0")
    assert "no '## 0.3.0 (YYYY-MM-DD)' section" in str(exc.value)
    assert "0.2.0 (2026-11-01)" in str(exc.value)


@pytest.mark.parametrize("changes", [
    "## 0.1.0\n\n- no date\n",
    "## 0.1.0 (October 2026)\n\n- bad date\n",
    "## 0.1.00 (2026-10-22)\n\n- other version\n",
])
def test_section_heading_must_match_exactly(changes):
    with pytest.raises(rc.ReleaseError):
        rc.release_notes(changes, "0.1.0")


def test_empty_section_fails():
    with pytest.raises(rc.ReleaseError, match="is empty"):
        rc.release_notes("## 0.1.0 (2026-10-22)\n\n## 0.0.9 (2026-10-01)\n- x\n", "0.1.0")


@pytest.mark.parametrize("version,expected", [
    ("0.1.0", True), ("0.9.9", True), ("1.0.0", False), ("2.3.4", False),
    ("1.0.0rc1", True), ("1.1.0b2", True), ("1.2.0a1", True), ("1.0.0.dev3", True),
])
def test_prerelease_below_1_0_0_or_pep440_prerelease(version, expected):
    assert rc.is_prerelease(version) is expected


def test_cli_success_writes_notes_and_github_outputs(tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    notes = tmp_path / "notes.md"
    assert rc.main(["--tag", f"v{VERSION}", "--notes-out", str(notes)]) == 0
    assert notes.read_text(encoding="utf-8").strip()
    assert f"version={VERSION}\n" in out.read_text(encoding="utf-8")
    assert "prerelease=true\n" in out.read_text(encoding="utf-8")  # 0.x


def test_cli_without_tag_checks_version_and_notes_only(tmp_path, monkeypatch):
    """Manual (TestPyPI) runs have no tag; the notes must still exist."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "CHANGES.md").write_text(CHANGES)
    notes = tmp_path / "notes.md"
    assert rc.main(["--root", str(tmp_path), "--notes-out", str(notes)]) == 0
    assert notes.read_text(encoding="utf-8") == "First release.\n\n- A tool.\n"

    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.3.0"\n')
    assert rc.main(["--root", str(tmp_path)]) == 1  # no CHANGES section for 0.3.0


def test_cli_failure_reports_a_github_error(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "CHANGES.md").write_text(CHANGES)
    assert rc.main(["--tag", "v0.1.1", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("::error title=Release check failed::")
    assert "v0.1.1" in err


def test_current_project_is_releasable():
    """The real pyproject.toml and CHANGES.md pass for the current version."""
    changes = (ROOT / "CHANGES.md").read_text(encoding="utf-8")
    rc.check_tag(f"v{VERSION}", VERSION)
    assert rc.release_notes(changes, VERSION).strip()


def test_release_notes_use_absolute_links():
    """Relative links break in a GitHub release body (they resolve against /releases)."""
    import re

    notes = rc.release_notes((ROOT / "CHANGES.md").read_text(encoding="utf-8"), VERSION)
    targets = re.findall(r"\]\(([^)]+)\)", notes)
    assert targets, "expected some links"
    assert [t for t in targets if not t.startswith("https://")] == []
