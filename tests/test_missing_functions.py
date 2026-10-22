"""Plugin functions the site lacks produce a clear message, never a half-build."""

import asyncio
import inspect

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fakes import moodle_error, site_info
from moodle_mcp_connector import server

P = server.PLUGIN_PREFIX

# Functions in the Phase 1 plugin release (2026102110), used to model a site
# whose plugin predates quizzes, forums, choices, assignments and backups.
PHASE1 = [P + n for n in (
    "get_info", "create_page", "create_book", "create_label", "create_url",
    "update_page", "create_section", "delete_section", "update_activity",
    "move_activity", "delete_activity",
)]


def call_tool(name: str, args: dict):
    """Call a tool the way an MCP client would."""
    return asyncio.run(server.mcp.call_tool(name, args))


def outline(*activities, courseid=0):
    o = {"sections": [{"name": "Week 1", "activities": list(activities)}]}
    if courseid:
        o["courseid"] = courseid
    else:
        o.update(fullname="Bio", shortname="bio101")
    return o


# --- _outline_functions (pure) --------------------------------------------- #

def test_outline_functions_lists_everything_once():
    o = outline(
        {"type": "page", "name": "A"},
        {"type": "page", "name": "B"},
        {"type": "quiz", "name": "Q", "questions": [
            {"qtype": "truefalse"}, {"name": "default is multichoice"}]},
    )
    assert server._outline_functions(o, rollback=True) == [
        ("creating the course", "core_course_create_courses"),
        ("rollback", "core_course_delete_courses"),
        ("sections", P + "create_section"),
        ("page", P + "create_page"),
        ("quiz", P + "create_quiz"),
        ("truefalse questions", P + "add_truefalse_question"),
        ("multichoice questions", P + "add_quiz_question"),
    ]


def test_outline_functions_existing_course_needs_no_course_functions():
    needed = [fn for _, fn in server._outline_functions(outline(courseid=7), rollback=True)]
    assert needed == [P + "create_section"]


def test_function_maps_cover_every_type_build_course_uses():
    assert set(server._ACTIVITY_FUNCTIONS) == server._ACTIVITY_TYPES
    assert set(server._QUESTION_FUNCTIONS) == server._QUESTION_TYPES
    # build_course must call exactly the functions the pre-check looks for.
    source = inspect.getsource(server._register_write_tools)
    for fn in [*server._ACTIVITY_FUNCTIONS.values(), *server._QUESTION_FUNCTIONS.values()]:
        assert f'"{fn}"' in source


# --- build_course pre-check -------------------------------------------------- #

def test_build_course_rejects_quiz_before_any_write(fake_call):
    fake = fake_call({"core_webservice_get_site_info": site_info(
        "core_course_create_courses", *PHASE1)})
    with pytest.raises(ToolError) as exc:
        call_tool("build_course", {"outline": outline(
            {"type": "page", "name": "Intro"},
            {"type": "quiz", "name": "Check", "questions": [{"qtype": "truefalse"}]},
        )})
    message = str(exc.value)
    assert ("quiz needs local_mcpconnector_create_quiz, which this site's plugin "
            "version does not provide") in message
    assert "truefalse questions needs local_mcpconnector_add_truefalse_question" in message
    assert "page needs" not in message
    assert [name for name, _ in fake.calls] == ["core_webservice_get_site_info"]


def test_build_course_reports_missing_core_function(fake_call):
    fake_call({"core_webservice_get_site_info": site_info(*PHASE1)})
    with pytest.raises(ToolError) as exc:
        call_tool("build_course", {"outline": outline({"type": "page", "name": "A"})})
    assert ("creating the course needs core_course_create_courses, which the "
            "token's web service does not include") in str(exc.value)


def test_build_course_proceeds_when_all_functions_exist(fake_call):
    fake = fake_call({
        "core_webservice_get_site_info": site_info(*PHASE1),
        P + "create_section": {"sectionid": 50, "sectionnum": 1},
        P + "create_page": {"cmid": 9, "instanceid": 3},
    })
    call_tool("build_course", {"outline": outline({"type": "page", "name": "A"}, courseid=7)})
    assert [name for name, _ in fake.calls] == [
        "core_webservice_get_site_info", P + "create_section", P + "create_page"]


# --- single-activity tools --------------------------------------------------- #

@pytest.mark.parametrize("errorcode", ["invalidrecord", "accessexception"])
def test_single_tool_missing_function_message(fake_http, errorcode):
    fake_http({
        P + "create_quiz": moodle_error(errorcode, "Can't find data record"),
        "core_webservice_get_site_info": site_info(*PHASE1),
    })
    with pytest.raises(ToolError) as exc:
        call_tool("create_quiz", {"courseid": 2, "name": "Q"})
    assert str(exc.value).endswith(
        "create_quiz needs local_mcpconnector_create_quiz, which this site's plugin "
        "version does not provide (or the token's web service does not include it).")


def test_access_error_for_available_function_is_left_alone(fake_http):
    # accessexception has other causes; if the function is listed, keep
    # Moodle's own message.
    fake_http({
        P + "create_page": moodle_error("accessexception", "Access control exception"),
        "core_webservice_get_site_info": site_info(*PHASE1),
    })
    with pytest.raises(server.MoodleError) as exc:
        asyncio.run(server._call(P + "create_page", {"courseid": 2}))
    assert str(exc.value).startswith("Moodle error [accessexception]: Access control exception")
    assert exc.value.errorcode == "accessexception"


def test_other_plugin_errors_are_left_alone(fake_http):
    fake = fake_http({P + "create_page": moodle_error("sectionnotfound", "Section not found")})
    with pytest.raises(server.MoodleError) as exc:
        asyncio.run(server._call(P + "create_page", {"courseid": 2, "section": 9}))
    assert "sectionnotfound" in str(exc.value)
    assert not fake.called("core_webservice_get_site_info")


def test_site_info_failure_keeps_original_error(fake_http):
    fake_http({
        P + "create_quiz": moodle_error("invalidrecord", "Can't find data record"),
        "core_webservice_get_site_info": moodle_error("invalidtoken", "Invalid token"),
    })
    with pytest.raises(server.MoodleError) as exc:
        asyncio.run(server._call(P + "create_quiz", {}))
    assert "[invalidrecord]" in str(exc.value)


def test_core_functions_are_not_rewritten(fake_http):
    fake = fake_http({"core_course_get_courses": moodle_error("accessexception", "Access")})
    with pytest.raises(server.MoodleError):
        asyncio.run(server._call("core_course_get_courses"))
    assert not fake.called("core_webservice_get_site_info")


def test_quiz_and_question_tools_stay_in_content_toolset():
    for name in ("create_quiz", "add_quiz_question", "add_truefalse_question",
                 "add_shortanswer_question", "add_essay_question",
                 "add_numerical_question", "add_matching_question"):
        assert name in server._CONTENT_WRITE_TOOLS
