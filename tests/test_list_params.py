"""List parameters are checked before calling Moodle.

_flatten cannot express an empty list, so [] would reach Moodle as a missing
parameter and come back as a confusing error. Each tool rejects lists that
are too short, with a clear message, before sending anything.
"""

import asyncio
import inspect
import json
import re

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import site_info
from moodle_mcp_connector import server

P = server.PLUGIN_PREFIX

# tool: (wsfunction, other required args, list param, minimum, one valid item)
CASES = {
    "list_quizzes": ("mod_quiz_get_quizzes_by_courses", {}, "courseids", 1, 3),
    "add_quiz_question": (P + "add_quiz_question",
                          {"quizcmid": 1, "name": "Q", "questiontext": "?"},
                          "answers", 2, {"text": "A", "fraction": 1.0}),
    "add_shortanswer_question": (P + "add_shortanswer_question",
                                 {"quizcmid": 1, "name": "Q", "questiontext": "?"},
                                 "answers", 1, "Paris"),
    "add_numerical_question": (P + "add_numerical_question",
                               {"quizcmid": 1, "name": "Q", "questiontext": "?"},
                               "answers", 1, {"answer": 42, "tolerance": 0}),
    "add_matching_question": (P + "add_matching_question",
                              {"quizcmid": 1, "name": "Q", "questiontext": "?"},
                              "pairs", 3, {"question": "France", "answer": "Paris"}),
    "create_choice": (P + "create_choice", {"courseid": 2, "name": "Poll", "question": "?"},
                      "options", 2, "Yes"),
    "enrol_users": ("enrol_manual_enrol_users", {}, "enrolments", 1,
                    {"userid": 3, "courseid": 2, "roleid": 5}),
    "add_group_members": ("core_group_add_group_members", {}, "members", 1,
                          {"groupid": 4, "userid": 3}),
    "delete_courses": ("core_course_delete_courses", {}, "courseids", 1, 9),
    "delete_users": ("core_user_delete_users", {}, "userids", 1, 9),
    "add_cohort_members": ("core_cohort_add_cohort_members", {"cohortid": 6}, "userids", 1, 3),
}


def call_tool(name: str, args: dict):
    return asyncio.run(server.mcp.call_tool(name, args))


def test_flatten_drops_empty_lists():
    # The reason these checks exist.
    assert server._flatten({"answers": [], "name": "Q"}) == {"name": "Q"}


@pytest.mark.parametrize("tool", CASES)
def test_too_short_list_is_rejected_before_calling_moodle(fake_call, tool):
    _, args, param, minimum, item = CASES[tool]
    fake = fake_call({})  # any Moodle call would fail the test
    with pytest.raises(ToolError) as exc:
        call_tool(tool, {**args, param: [item] * (minimum - 1)})
    message = str(exc.value)
    assert f"{param} needs at least {minimum}" in message
    assert "nothing was sent to Moodle" in message
    assert fake.calls == []


# Moodle returns null for these, and the tools are annotated -> None.
RETURNS_NULL = {"enrol_users", "add_group_members", "delete_users"}
# The Moodle parameter a tool sends its list as, where the names differ.
SENT_AS = {"add_cohort_members": "members"}


@pytest.mark.parametrize("tool", CASES)
def test_minimum_length_list_is_sent(fake_call, tool):
    wsfunction, args, param, minimum, item = CASES[tool]
    fake = fake_call({wsfunction: None if tool in RETURNS_NULL else {}})
    call_tool(tool, {**args, param: [item] * minimum})
    [(name, params)] = fake.calls
    assert name == wsfunction
    sent = SENT_AS.get(tool, param)
    keys = server._flatten(params)
    for i in range(minimum):
        assert any(k.startswith(f"{sent}[{i}]") for k in keys), (sent, i, keys)


@pytest.mark.parametrize("activity,problem", [
    ({"type": "choice", "name": "Poll", "options": ["Only one"]},
     "options needs at least 2 items, got 1"),
    ({"type": "choice", "name": "Poll"}, "options must be a list of at least 2"),
    ({"type": "quiz", "name": "Q", "questions": [
        {"qtype": "multichoice", "answers": [{"text": "A", "fraction": 1}]}]},
     "questions[0].answers needs at least 2 items, got 1"),
    ({"type": "quiz", "name": "Q", "questions": [{"name": "default multichoice"}]},
     "questions[0].answers must be a list of at least 2"),
    ({"type": "quiz", "name": "Q", "questions": [{"qtype": "shortanswer", "answers": []}]},
     "questions[0].answers needs at least 1 item, got 0"),
    ({"type": "quiz", "name": "Q", "questions": [{"qtype": "numerical", "answers": []}]},
     "questions[0].answers needs at least 1 item, got 0"),
    ({"type": "quiz", "name": "Q", "questions": [
        {"qtype": "matching", "pairs": [{"question": "a", "answer": "b"},
                                        {"question": "c", "answer": "d"}]}]},
     "questions[0].pairs needs at least 3 items, got 2"),
])
def test_build_course_rejects_short_lists_before_any_call(fake_call, activity, problem):
    fake = fake_call({})
    outline = {"courseid": 7, "sections": [{"name": "W1", "activities": [activity]}]}
    with pytest.raises(ToolError) as exc:
        call_tool("build_course", {"outline": outline})
    assert "Invalid course outline" in str(exc.value)
    assert problem in str(exc.value)
    assert fake.calls == []


def test_build_course_accepts_lists_at_minimum(fake_call):
    fake = fake_call({
        "core_webservice_get_site_info": site_info(
            P + "create_section", P + "create_choice", P + "create_quiz",
            P + "add_quiz_question", P + "add_matching_question",
            P + "add_truefalse_question"),
        P + "create_section": {"sectionid": 1, "sectionnum": 1},
        P + "create_choice": {"cmid": 2, "instanceid": 2},
        P + "create_quiz": {"cmid": 3, "instanceid": 3},
        P + "add_quiz_question": {},
        P + "add_matching_question": {},
        P + "add_truefalse_question": {},
    })
    outline = {"courseid": 7, "sections": [{"name": "W1", "activities": [
        {"type": "choice", "name": "Poll", "options": ["Yes", "No"]},
        {"type": "quiz", "name": "Q", "questions": [
            {"qtype": "multichoice", "answers": [{"text": "A", "fraction": 1}, {"text": "B", "fraction": 0}]},
            {"qtype": "matching", "pairs": [{"question": "a", "answer": "b"},
                                            {"question": "c", "answer": "d"},
                                            {"question": "e", "answer": "f"}]},
            {"qtype": "truefalse", "correct": 1},  # needs no list
        ]},
    ]}]}
    call_tool("build_course", {"outline": outline})
    assert [name for name, _ in fake.calls][-3:] == [
        P + "add_quiz_question", P + "add_matching_question", P + "add_truefalse_question"]


def test_build_course_docstring_example_is_a_valid_outline():
    """The example agents copy from must pass validation (e.g. 3 matching pairs)."""
    doc = inspect.getsource(server._register_write_tools)
    example = doc.split("`outline` shape:", 1)[1].split("Creates the course", 1)[0]
    example = re.sub(r"#[^\n]*", "", example)  # drop the "# optional" comments
    outline = json.loads(example)
    assert server._validate_outline(outline) == []
    [matching] = [q for q in outline["sections"][0]["activities"][-1]["questions"]
                  if q["qtype"] == "matching"]
    assert len(matching["pairs"]) == 3
