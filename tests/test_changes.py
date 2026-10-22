"""CHANGES.md has a section for the current version that matches the code."""

import asyncio
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from moodle_mcp_connector import server

ROOT = Path(__file__).resolve().parent.parent
CHANGES = (ROOT / "CHANGES.md").read_text(encoding="utf-8")
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
PLUGIN_RELEASES = "https://github.com/Teci-AI/moodle-local_mcpconnector/releases"


def section(version: str) -> str:
    match = re.search(rf"^## {re.escape(version)} \((\d{{4}}-\d{{2}}-\d{{2}})\)\n(.*?)(?=^## |\Z)",
                      CHANGES, re.M | re.S)
    assert match, f"CHANGES.md has no '## {version} (YYYY-MM-DD)' section"
    return match.group(2)


def test_current_version_has_a_dated_section():
    assert section(VERSION).strip()


def test_plugin_requirement_matches_the_code():
    text = section(VERSION)
    assert str(server.MIN_PLUGIN_VERSION) in text
    assert PLUGIN_RELEASES in text


def test_tool_counts_match_the_code():
    def count(env: dict) -> int:
        code = ("import asyncio; from moodle_mcp_connector import server; "
                "print(len(asyncio.run(server.mcp.list_tools())))")
        env = {**os.environ, "MOODLE_URL": "", "MOODLE_TOKEN": "", **env}
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT,
                             capture_output=True, text=True, check=True)
        return int(out.stdout.strip())

    text = " ".join(section(VERSION).split())
    read = count({"MOODLE_ALLOW_WRITE": "", "MOODLE_TOOLSET": ""})
    total = count({"MOODLE_ALLOW_WRITE": "true", "MOODLE_TOOLSET": ""})
    content_read = count({"MOODLE_ALLOW_WRITE": "", "MOODLE_TOOLSET": "content"})
    content_all = count({"MOODLE_ALLOW_WRITE": "true", "MOODLE_TOOLSET": "content"})
    assert f"{total} tools" in text and f"{read} read tools" in text
    assert f"{total - read} write tools" in text
    assert f"{content_read} read tools, plus {content_all - content_read} write tools" in text
