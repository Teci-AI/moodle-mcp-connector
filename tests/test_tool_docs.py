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
    "create_quiz": '{"cmid": int, "instanceid": int}',
    "create_forum": '{"cmid": int, "instanceid": int}',
    "create_choice": '{"cmid": int, "instanceid": int}',
    "create_assignment": '{"cmid": int, "instanceid": int}',
    "add_quiz_question": '{"questionid": int, "slot": int}',
    "add_truefalse_question": '{"questionid": int, "slot": int}',
    "add_shortanswer_question": '{"questionid": int, "slot": int}',
    "add_essay_question": '{"questionid": int, "slot": int}',
    "add_numerical_question": '{"questionid": int, "slot": int}',
    "add_matching_question": '{"questionid": int, "slot": int}',
    "restore_course": '{"courseid": int, "fullname": str, "shortname": str}',
    # The tool saves the plugin's base64 file and reports where it went.
    "backup_course": '{"saved_to": str, "size": int, "filename": str}',
}

QUESTION_TOOLS = [n for n in RETURNS if n.startswith("add_") and n.endswith("_question")]
FRACTION_VALUES = ("1.0, 0.9, 0.8333333, 0.8, 0.75, 0.7, 0.6666667, 0.6, 0.5, 0.4, "
                   "0.3333333, 0.3, 0.25, 0.2, 0.1666667, 0.1428571, 0.125, 0.1111111, "
                   "0.1, 0.05, 0, or their negatives")

BEHAVIOUR = {
    "create_page": ["sectionnotfound"],
    "create_book": ["sectionnotfound"],
    "create_label": ["sectionnotfound", "built from the label's text"],
    "create_url": ["sectionnotfound", "0 (automatic) is the safe", "enabled in the site's URL settings"],
    "move_activity": ["sectionnotfound", "END of the target section"],
    "delete_section": ["section 0 cannot be deleted"],
    "add_quiz_question": [
        FRACTION_VALUES, "within 0.005", "0.33 counts as a third",
        "With `single=1` (one correct answer) one answer must have fraction 1.0",
        "the positive fractions must add up to 1.0",
        'answers without "fraction" are worth 0',
    ],
    "add_numerical_question": [
        FRACTION_VALUES, "within 0.005",
        "One answer must have fraction 1.0 (the default", "tolerance must be 0 or more",
    ],
    "add_essay_question": [
        "must be one of 2, 3, 5, 10, 15, 20, 25, 30, 35, 40",
        "must be one of 0, 1, 2, 3, or -1",
    ],
    "add_matching_question": ["At least three pairs"],
    "create_quiz": ["`timeclose` must not be before `timeopen`", "take the site's quiz defaults"],
    "create_forum": ["take the site's forum defaults"],
    "create_assignment": [
        "take the site's assignment defaults",
        "between 0 and the site's maximum grade (100 by default)",
    ],
    "create_choice": ["`question` becomes the choice's description", "at least two"],
    "backup_course": [
        '{"filename": str, "size": int, "content_base64": str}',
        "backup-moodle2-course-12-bio101-20261021-1200-nu.mbz",
        "moodle/backup:userinfo", "managers", "over 100 MB are refused",
    ],
    "restore_course": [
        "moodle/course:create", "moodle/restore:restorecourse", "moodle/restore:uploadfile",
        "category manager, not a teacher", "never overwrites an existing course",
        '" copy 1"', '"_1"', "report the names from the result",
        "post_max_size", "4/3 of the .mbz size",
    ],
}


@pytest.mark.parametrize("name", QUESTION_TOOLS)
def test_question_tools_document_placement_and_attempt_lock(descriptions, name):
    text = descriptions[name]
    assert "added at the end of the quiz, in the quiz's own question bank" in text
    assert '"cannoteditafterattempts"' in text


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
