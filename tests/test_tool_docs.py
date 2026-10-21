"""Tool descriptions document the plugin's fixed return values and behaviour.

Agents only see these descriptions, so they must name the exact keys the
local_mcpconnector plugin returns.
"""

import asyncio
from pathlib import Path

import pytest

from moodle_mcp_connector import server

RETURNS = {
    "create_page": '{"cmid": int, "instanceid": int}',
    "create_label": '{"cmid": int, "instanceid": int}',
    "create_url": '{"cmid": int, "instanceid": int}',
    "create_book": '{"cmid": int, "instanceid": int, "chapterid": int}',
    "update_page": '{"cmid": int, "success": bool}',
    "create_section": '{"sectionid": int, "sectionnum": int}',
    "delete_section": '{"success": bool}',
    "update_activity": '{"cmid": int, "name": str, "visible": int, "changed": [...]}',
    "move_activity": '{"cmid": int, "section": int}',
    "delete_activity": '{"cmid": int, "success": bool}',
}

BEHAVIOUR = {
    "create_page": ["sectionnotfound"],
    "create_book": ["sectionnotfound"],
    "create_label": ["sectionnotfound", "built from the label's text"],
    "create_url": ["sectionnotfound", "0 (automatic) is the safe", "enabled in the site's URL settings"],
    "move_activity": ["sectionnotfound", "END of the target section"],
    "delete_section": ["section 0 cannot be deleted"],
}


@pytest.fixture(scope="module")
def descriptions() -> dict:
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name: " ".join((t.description or "").split()) for t in tools}


NOW_PROVIDED = [
    "create_quiz", "add_quiz_question", "add_truefalse_question",
    "add_shortanswer_question", "add_essay_question", "add_numerical_question",
    "add_matching_question", "create_forum", "create_choice", "create_assignment",
    "backup_course", "restore_course",
]


def test_no_tool_claims_its_function_is_missing(descriptions):
    for name, text in descriptions.items():
        for stale in ("NOTE: this function", "next release", "current release",
                      "Until the plugin adds it"):
            assert stale not in text, f"{name}: {stale!r}"


@pytest.mark.parametrize("name", NOW_PROVIDED)
def test_formerly_missing_tools_still_name_the_plugin(descriptions, name):
    assert "Requires the local_mcpconnector plugin." in descriptions[name]


def test_readme_marks_no_function_as_missing():
    readme = (Path(server.__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "planned next" not in readme
    assert "not yet available" not in readme


@pytest.mark.parametrize("name,returns", RETURNS.items())
def test_return_keys_documented(descriptions, name, returns):
    assert f"Returns {returns}" in descriptions[name]


@pytest.mark.parametrize("name,phrases", BEHAVIOUR.items())
def test_behaviour_documented(descriptions, name, phrases):
    for phrase in phrases:
        assert phrase in descriptions[name], f"{name}: missing {phrase!r}"
