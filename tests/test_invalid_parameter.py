"""The plugin's invalidparameter errors reach the agent with their reason intact.

local_mcpconnector rejects bad input with errorcode "invalidparameter" and a
message stating why ("Invalid parameter value: ..."). The agent needs that
reason verbatim to fix its call, so nothing on the way may rewrite or drop
it. These tests send a mocked Moodle response through the real _call.
"""

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fakes import site_info
from moodle_mcp_connector import server

P = server.PLUGIN_PREFIX
REASON = ("Invalid parameter value: answers[1][fraction] 0.42 is not a valid "
          "grade value (use e.g. 0.5, 0.3333333 or 0.25)")


def invalid(message: str = REASON) -> dict:
    return {"exception": "invalid_parameter_exception",
            "errorcode": "invalidparameter", "message": message}


def call_tool(name: str, args: dict):
    return asyncio.run(server.mcp.call_tool(name, args))


QUESTION_ARGS = {
    "quizcmid": 4, "name": "Q1", "questiontext": "Pick one",
    "answers": [{"text": "A", "fraction": 1.0}, {"text": "B", "fraction": 0.42}],
}


def test_call_keeps_message_and_errorcode(fake_http):
    fake_http({P + "add_quiz_question": invalid()})
    with pytest.raises(server.MoodleError) as exc:
        asyncio.run(server._call(P + "add_quiz_question", QUESTION_ARGS))
    assert exc.value.errorcode == "invalidparameter"
    assert str(exc.value) == f"Moodle error [invalidparameter]: {REASON}"


def test_tool_error_shows_the_reason_verbatim(fake_http):
    fake = fake_http({P + "add_quiz_question": invalid()})
    with pytest.raises(ToolError) as exc:
        call_tool("add_quiz_question", QUESTION_ARGS)
    assert str(exc.value).endswith(f"Moodle error [invalidparameter]: {REASON}")
    # Not mistaken for a missing function: no function-list lookup, no rewrite.
    assert not fake.called("core_webservice_get_site_info")
    assert "needs local_mcpconnector" not in str(exc.value)


@pytest.mark.parametrize("message", [
    "Invalid parameter value: responselines must be one of 2, 3, 5, 10, 15, 20, 25, 30, 35, 40",
    'Invalid parameter value: name "Café — été" contains <b>markup</b> & {braces}',
])
def test_unusual_characters_survive(fake_http, message):
    fake_http({P + "add_essay_question": invalid(message)})
    with pytest.raises(ToolError) as exc:
        call_tool("add_essay_question", {"quizcmid": 4, "name": "E", "questiontext": "Why?",
                                         "responselines": 7})
    assert message in str(exc.value)


def outline() -> dict:
    return {"courseid": 7, "sections": [{"name": "W1", "activities": [
        {"type": "quiz", "name": "Check", "questions": [
            {"qtype": "multichoice", "name": "Q1", "text": "?",
             "answers": [{"text": "A", "fraction": 1.0}, {"text": "B", "fraction": 0.42}]}]},
        {"type": "page", "name": "After", "content": "<p>x</p>"},
    ]}]}


def build_responses() -> dict:
    return {
        "core_webservice_get_site_info": site_info(
            P + "create_section", P + "create_quiz", P + "add_quiz_question",
            P + "create_page"),
        P + "create_section": {"sectionid": 1, "sectionnum": 1},
        P + "create_quiz": {"cmid": 4, "instanceid": 2},
        P + "add_quiz_question": invalid(),
        P + "create_page": {"cmid": 5, "instanceid": 3},
    }


def test_build_course_logs_the_reason_and_continues(fake_http):
    fake_http(build_responses())
    result = asyncio.run(server.mcp.call_tool("build_course", {"outline": outline()}))
    if isinstance(result, tuple):
        result = result[0]
    report = json.loads(result[0].text)
    [error] = [entry for entry in report["built"] if "error" in entry]
    assert error["error"] == f"Moodle error [invalidparameter]: {REASON}"
    assert report["ok"] is False
    assert any(entry.get("created") == "page" for entry in report["built"])


def test_build_course_rollback_raises_the_reason(fake_http):
    fake_http(build_responses())
    with pytest.raises(ToolError) as exc:
        call_tool("build_course", {"outline": outline(), "rollback": True})
    assert REASON in str(exc.value)
    assert "rolled back" in str(exc.value)
