"""Check a release tag against the project and extract its release notes.

Usage: python scripts/release_check.py [--tag v0.1.0] [--notes-out FILE]

Fails with a clear message unless the tag (when given) is exactly "v" + the
version in pyproject.toml and CHANGES.md has a non-empty
"## <version> (YYYY-MM-DD)" section. Without --tag, as for a manual TestPyPI
run, only the version and the release notes are checked. On success it writes that section to FILE (the GitHub release
notes) and reports whether the release is a pre-release: any version below
1.0.0, or a PEP 440 pre-release such as 1.2.0rc1. When run in GitHub Actions
it also sets the step outputs `version` and `prerelease`.

Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReleaseError(Exception):
    """The tag, the version and CHANGES.md do not agree."""


def project_version(pyproject: str) -> str:
    return tomllib.loads(pyproject)["project"]["version"]


def check_tag(tag: str, version: str) -> None:
    if tag != f"v{version}":
        raise ReleaseError(
            f"tag {tag!r} does not match the version in pyproject.toml ({version}); "
            f"the tag for this version must be 'v{version}'. Fix the tag or bump the "
            "version, then tag again.")


def release_notes(changes: str, version: str) -> str:
    """The body of CHANGES.md's '## <version> (YYYY-MM-DD)' section."""
    heading = re.compile(rf"^## {re.escape(version)} \(\d{{4}}-\d{{2}}-\d{{2}}\)[ \t]*$", re.M)
    match = heading.search(changes)
    if not match:
        found = re.findall(r"^## (.+)$", changes, re.M)
        raise ReleaseError(
            f"CHANGES.md has no '## {version} (YYYY-MM-DD)' section "
            f"(sections found: {found or 'none'}).")
    rest = changes[match.end():]
    following = re.search(r"^## ", rest, re.M)
    notes = (rest[:following.start()] if following else rest).strip()
    if not notes:
        raise ReleaseError(f"the CHANGES.md section for {version} is empty.")
    return notes + "\n"


def is_prerelease(version: str) -> bool:
    """Below 1.0.0, or a PEP 440 pre-release/dev version."""
    match = re.match(r"^(\d+)(?:\.\d+)*((?:a|b|rc|\.dev)\d*)?", version)
    if not match:
        raise ReleaseError(f"cannot parse version {version!r}")
    return int(match.group(1)) < 1 or bool(match.group(2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a release tag and extract its notes.")
    parser.add_argument("--tag", help="the pushed tag, e.g. v0.1.0; omit for a manual "
                                      "(TestPyPI) run, which checks only the version and notes")
    parser.add_argument("--notes-out", type=Path, help="where to write the release notes")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        version = project_version((args.root / "pyproject.toml").read_text(encoding="utf-8"))
        if args.tag is not None:
            check_tag(args.tag, version)
        notes = release_notes((args.root / "CHANGES.md").read_text(encoding="utf-8"), version)
        prerelease = is_prerelease(version)
    except ReleaseError as exc:
        # ::error:: makes GitHub show the message on the run summary.
        print(f"::error title=Release check failed::{exc}", file=sys.stderr)
        return 1

    if args.notes_out:
        args.notes_out.write_text(notes, encoding="utf-8")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"version={version}\nprerelease={'true' if prerelease else 'false'}\n")
    print(f"Release {args.tag or '(no tag: manual run)'}: version {version}, "
          f"{'pre-release' if prerelease else 'full release'}, "
          f"{len(notes.splitlines())} lines of notes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
