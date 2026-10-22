"""Live tests: every plugin tool against a real Moodle, through the MCP layer.

Each run creates its own hidden throwaway course (shortname
mcptest-<timestamp>-<random>-main) in MOODLE_TEST_CATEGORYID and deletes it
at the end, together with every other course the run creates (built and
restored courses), even when tests fail. Teardown only ever deletes courses
whose shortname starts with "mcptest-" and that this run registered.

Run with:  pytest tests/integration -v   (see CONTRIBUTING.md for the setup)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from moodle_mcp_connector import server

PREFIX = "mcptest-"


# --------------------------------------------------------------------------- #
# Calling tools the way an MCP client does.
# --------------------------------------------------------------------------- #

def call(tool: str, /, *, as_list: bool = False, **args):
    """Call a tool through FastMCP and decode its JSON result.

    `tool` is positional-only so tools can take a `name` argument. FastMCP
    returns a list result as one content block per item, so callers of
    list-returning tools pass as_list=True. Fails, without printing the
    token, if the token appears in a result or an error.
    """
    token = server.MOODLE_TOKEN
    try:
        result = asyncio.run(server.mcp.call_tool(tool, args))
    except ToolError as exc:
        if token in str(exc):
            pytest.fail(f"the token appeared in an error from {tool}")
        raise
    content, structured = result if isinstance(result, tuple) else (result, None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        value = structured["result"]
    else:
        values = [json.loads(block.text) for block in content]
        value = values if as_list else (values[0] if values else None)
    if token in json.dumps(value):
        pytest.fail(f"the token appeared in the result of {tool}")
    return value


def modules(courseid: int) -> list[tuple[int, dict]]:
    """(section number, module) for every module in a course, in order."""
    sections = call("get_course_content", as_list=True, courseid=courseid)
    return [(s["section"], m) for s in sections for m in s.get("modules", [])]


def find_courses(field: str, value: str) -> list[dict]:
    return call("search_courses", field=field, value=value).get("courses", [])


def assert_ids(result: dict, *keys: str) -> None:
    for key in keys:
        assert isinstance(result.get(key), int) and result[key] > 0, (key, result)


# --------------------------------------------------------------------------- #
# Throwaway courses and their guaranteed cleanup.
# --------------------------------------------------------------------------- #

class Courses:
    """Every course this run may create, so teardown can delete exactly those."""

    def __init__(self) -> None:
        self.stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        self.shortnames: list[str] = []
        self.ids: set[int] = set()

    def shortname(self, suffix: str) -> str:
        """A unique shortname, registered for cleanup before anything is created."""
        name = f"{PREFIX}{self.stamp}-{suffix}"
        self.shortnames.append(name)
        return name

    def track(self, courseid: int) -> None:
        self.ids.add(int(courseid))

    def cleanup(self) -> list[str]:
        """Delete this run's courses; return problems (empty means all gone)."""
        problems: list[str] = []
        found: dict[int, str] = {}
        lookups = [("shortname", s) for s in self.shortnames]
        lookups += [("id", str(i)) for i in sorted(self.ids)]
        for field, value in lookups:
            try:
                for course in find_courses(field, value):
                    found[course["id"]] = course["shortname"]
            except Exception as exc:  # noqa: BLE001 - keep cleaning up
                problems.append(f"could not look up course {field}={value}: {exc}")
        unsafe = {cid: name for cid, name in found.items() if not name.startswith(PREFIX)}
        for cid, name in unsafe.items():
            problems.append(f"refusing to delete course {cid} ({name!r}): not a test course")
        doomed = sorted(set(found) - set(unsafe))
        if doomed:
            try:
                call("delete_courses", courseids=doomed)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"delete_courses failed: {exc}")
            for cid in doomed:
                try:
                    if find_courses("id", str(cid)):
                        problems.append(f"course {cid} ({found[cid]}) still exists")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"could not confirm course {cid} was deleted: {exc}")
        return problems


@pytest.fixture(scope="module")
def courses(live, categoryid):
    registry = Courses()
    yield registry
    problems = registry.cleanup()
    if problems:
        pytest.fail("cleanup incomplete; delete these by hand:\n" + "\n".join(problems))


@pytest.fixture(scope="module")
def course(courses, categoryid) -> int:
    """The run's hidden throwaway course."""
    shortname = courses.shortname("main")
    created = call("create_course", as_list=True, fullname=f"MCP connector test {courses.stamp}",
                   shortname=shortname, categoryid=categoryid, visible=0,
                   summary="Created by the moodle-mcp-connector live tests; deleted at the end.")
    courseid = created[0]["id"]
    courses.track(courseid)
    return courseid


@pytest.fixture(scope="module")
def sections(course) -> tuple[int, int]:
    """Two sections created by the run: (activities, move target)."""
    first = call("create_section", courseid=course, name="Live test activities")
    second = call("create_section", courseid=course, name="Live test move target")
    return first["sectionnum"], second["sectionnum"]


# --------------------------------------------------------------------------- #
# Tests (run in file order; they share the module's course).
# --------------------------------------------------------------------------- #

def test_server_info_reports_a_compatible_plugin(live):
    info = call("server_info")
    assert info["connected"] is True, info.get("error")
    assert info["plugin_installed"] is True
    assert info["plugin_compatible"] is True, (
        f"plugin version {info['plugin_version']} is older than {info['plugin_min_version']}")


def test_create_and_delete_section(course):
    created = call("create_section", courseid=course, name="Live test temporary")
    assert_ids(created, "sectionid", "sectionnum")
    names = {s["name"] for s in call("get_course_content", as_list=True, courseid=course)}
    assert "Live test temporary" in names

    deleted = call("delete_section", courseid=course, sectionnum=created["sectionnum"])
    assert deleted["success"] is True
    names = {s["name"] for s in call("get_course_content", as_list=True, courseid=course)}
    assert "Live test temporary" not in names


def test_create_read_and_update_page(course, sections):
    page = call("create_page", courseid=course, section=sections[0], name="Live page",
                content="<p>Hello from the <b>live</b> test.</p>")
    assert_ids(page, "cmid", "instanceid")
    text = call("read_activity", course_id=course, cmid=page["cmid"])["text"]
    assert "Hello from the live test." in text

    updated = call("update_page", cmid=page["cmid"], content="<p>Updated page text.</p>")
    assert updated["cmid"] == page["cmid"] and updated["success"] is True
    text = call("read_activity", course_id=course, cmid=page["cmid"])["text"]
    assert "Updated page text." in text and "Hello from" not in text


def test_create_and_read_book(course, sections):
    book = call("create_book", courseid=course, section=sections[0], name="Live book",
                chaptertitle="Chapter one", chaptercontent="<p>Book chapter text.</p>")
    assert_ids(book, "cmid", "instanceid", "chapterid")
    read = call("read_activity", course_id=course, cmid=book["cmid"])
    assert read["chapters"][0]["title"] == "Chapter one"
    assert "Book chapter text." in read["text"]


def test_create_and_read_label(course, sections):
    label = call("create_label", courseid=course, section=sections[0],
                 content="<p>Label text for the live test.</p>")
    assert_ids(label, "cmid", "instanceid")
    assert "Label text for the live test." in call(
        "read_activity", course_id=course, cmid=label["cmid"])["text"]
    [mod] = [m for _, m in modules(course) if m["id"] == label["cmid"]]
    assert mod["name"].strip(), "a label with no name should get one built from its text"


@pytest.mark.parametrize("tool,modname,args", [
    ("create_url", "url", {"name": "Live URL", "externalurl": "https://moodle.org", "display": 0}),
    ("create_forum", "forum", {"name": "Live forum", "type": "general"}),
    ("create_choice", "choice", {"name": "Live choice", "question": "Which one?",
                                 "options": ["Yes", "No"]}),
    ("create_assignment", "assign", {"name": "Live assignment", "grade": 50,
                                     "duedate": int(time.time()) + 7 * 86400}),
])
def test_create_other_activities(course, sections, tool, modname, args):
    result = call(tool, courseid=course, section=sections[0], **args)
    assert_ids(result, "cmid", "instanceid")
    [(_, mod)] = [(s, m) for s, m in modules(course) if m["id"] == result["cmid"]]
    assert mod["modname"] == modname and mod["name"] == args["name"]


def test_update_move_and_delete_activity(course, sections):
    first, target = sections
    call("create_label", courseid=course, section=target, content="<p>Already here.</p>")
    page = call("create_page", courseid=course, section=first, name="Movable page",
                content="<p>Move me.</p>")
    cmid = page["cmid"]

    updated = call("update_activity", cmid=cmid, name="Renamed page", visible=0)
    assert updated["cmid"] == cmid
    assert updated["name"] == "Renamed page" and updated["visible"] == 0
    assert set(updated["changed"]) == {"name", "visible"}

    moved = call("move_activity", cmid=cmid, section=target)
    assert moved["cmid"] == cmid and moved["section"] == target
    in_target = [m["id"] for s, m in modules(course) if s == target]
    assert in_target[-1] == cmid, "the activity should be placed at the end of the section"

    deleted = call("delete_activity", cmid=cmid)
    assert deleted["cmid"] == cmid and deleted["success"] is True
    assert cmid not in [m["id"] for _, m in modules(course)]


def test_quiz_with_every_question_type(course, sections):
    quiz = call("create_quiz", courseid=course, section=sections[0], name="Live quiz")
    assert_ids(quiz, "cmid", "instanceid")
    common = {"quizcmid": quiz["cmid"], "questiontext": "<p>Question?</p>"}
    added = [
        call("add_quiz_question", name="Multiple choice", **common,
             answers=[{"text": "Right", "fraction": 1.0}, {"text": "Wrong", "fraction": 0.0}]),
        call("add_truefalse_question", name="True or false", **common, correctanswer=1),
        call("add_shortanswer_question", name="Short answer", **common, answers=["Paris"]),
        call("add_essay_question", name="Essay", **common, responselines=10, attachments=0),
        call("add_numerical_question", name="Numerical", **common,
             answers=[{"answer": 42, "tolerance": 0.5}]),
        call("add_matching_question", name="Matching", **common, pairs=[
            {"question": "France", "answer": "Paris"},
            {"question": "Italy", "answer": "Rome"},
            {"question": "Spain", "answer": "Madrid"}]),
    ]
    for result in added:
        assert_ids(result, "questionid", "slot")
    assert [r["slot"] for r in added] == [1, 2, 3, 4, 5, 6]
    assert len({r["questionid"] for r in added}) == 6


def test_rejected_call_reports_the_reason(course, sections):
    quiz = call("create_quiz", courseid=course, section=sections[0], name="Live rejected quiz")
    with pytest.raises(ToolError) as exc:
        call("add_quiz_question", quizcmid=quiz["cmid"], name="Bad fraction",
             questiontext="<p>?</p>",
             answers=[{"text": "A", "fraction": 1.0}, {"text": "B", "fraction": 0.37}])
    message = str(exc.value)
    assert "[invalidparameter]" in message
    assert "Invalid parameter value:" in message


def build_outline(courses, categoryid, suffix: str, quiz_questions: list[dict]) -> dict:
    return {
        "fullname": f"MCP connector test {courses.stamp} {suffix}",
        "shortname": courses.shortname(suffix),
        "categoryid": categoryid,
        "sections": [{"name": "Week 1", "activities": [
            {"type": "page", "name": "Built page", "content": "<p>Built.</p>"},
            {"type": "label", "text": "<p>Built label.</p>"},
            {"type": "url", "name": "Built URL", "url": "https://moodle.org"},
            {"type": "quiz", "name": "Built quiz", "questions": quiz_questions},
        ]}],
    }


GOOD_QUESTIONS = [
    {"qtype": "truefalse", "name": "TF", "text": "<p>Sky is blue?</p>", "correct": 1},
    {"qtype": "matching", "name": "Match", "text": "<p>Match.</p>", "pairs": [
        {"question": "France", "answer": "Paris"}, {"question": "Italy", "answer": "Rome"},
        {"question": "Spain", "answer": "Madrid"}]},
]


@pytest.mark.parametrize("rollback", [False, True], ids=["no-rollback", "rollback"])
def test_build_course(courses, categoryid, rollback):
    outline = build_outline(courses, categoryid, f"build-{int(rollback)}", GOOD_QUESTIONS)
    result = call("build_course", outline=outline, rollback=rollback)
    courses.track(result["courseid"])
    assert result["ok"] is True, [e for e in result["built"] if "error" in e]
    assert result["rolledback"] is False
    names = {m["name"] for _, m in modules(result["courseid"])}
    assert {"Built page", "Built URL", "Built quiz"} <= names


def test_build_course_rollback_leaves_no_course(courses, categoryid):
    bad = [{"qtype": "multichoice", "name": "Bad", "text": "<p>?</p>",
            "answers": [{"text": "A", "fraction": 1.0}, {"text": "B", "fraction": 0.37}]}]
    outline = build_outline(courses, categoryid, "rollback-fail", bad)
    with pytest.raises(ToolError) as exc:
        call("build_course", outline=outline, rollback=True)
    message = str(exc.value)
    assert "rolled back (course deleted)" in message
    assert "Invalid parameter value:" in message
    assert find_courses("shortname", outline["shortname"]) == []


def test_backup_and_restore(course, courses, categoryid, tmp_path: Path):
    backup = call("backup_course", courseid=course, save_to=str(tmp_path / "live.mbz"))
    saved = Path(backup["saved_to"])
    assert saved.is_file() and saved.stat().st_size == backup["size"] > 0
    assert backup["filename"].endswith(".mbz")

    shortname = courses.shortname("restored")
    restored = call("restore_course", filepath=str(saved), categoryid=categoryid,
                    fullname=f"MCP connector test {courses.stamp} restored", shortname=shortname)
    courses.track(restored["courseid"])
    assert restored["shortname"].startswith(shortname)
    names = {m["name"] for _, m in modules(restored["courseid"])}
    assert {"Live page", "Live book", "Live quiz"} <= names
