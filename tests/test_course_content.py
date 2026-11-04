"""get_course_content returns a compact outline unless asked for Moodle's full data.

Moodle's full reply for a 30-section course is over 100 KB, which MCP clients
cut before the model sees it, so the default must stay small.
"""

import asyncio
import json

from moodle_mcp_connector import server


def section(number: int, name: str, *modules: dict) -> dict:
    """A section as core_course_get_contents returns it, with the bulky fields."""
    return {
        "id": 100 + number, "name": name, "visible": 1, "summary": "<p>" + "x" * 500 + "</p>",
        "summaryformat": 1, "section": number, "hiddenbynumsections": 0,
        "uservisible": True, "component": None, "itemid": None, "modules": list(modules),
    }


def module(cmid: int, name: str, modname: str = "page") -> dict:
    return {
        "id": cmid, "url": f"https://moodle.test/mod/{modname}/view.php?id={cmid}",
        "name": name, "instance": cmid + 1000, "contextid": cmid + 2000, "visible": 1,
        "uservisible": True, "visibleoncoursepage": 1, "modicon": "icon.svg",
        "modname": modname, "modplural": modname + "s", "availability": None,
        "indent": 0, "onclick": "", "afterlink": None, "customdata": '""',
        "noviewlink": False, "completion": 0, "downloadcontent": 1, "dates": [],
        "groupmode": 0, "contents": [{"type": "file", "filename": "index.html"}],
    }


COURSE = [
    section(0, "Welcome", module(5, "News", "forum")),
    section(1, "Chapter 1 - Agency &amp; Disclosures", module(8, "Reading", "lesson"),
            module(9, "Quiz 1", "quiz")),
    section(2, "Final Exam"),
]


def run(fake_call, **kwargs):
    fake = fake_call({"core_course_get_contents": COURSE})
    result = asyncio.run(server.get_course_content(3, **kwargs))
    assert fake.calls == [("core_course_get_contents", {"courseid": 3})]
    return result


def test_default_is_a_compact_outline(fake_call):
    result = run(fake_call)
    assert result[1] == {
        "section": 1, "id": 101, "name": "Chapter 1 - Agency & Disclosures", "visible": 1,
        "modules": [
            {"id": 8, "name": "Reading", "modname": "lesson", "instance": 1008, "visible": 1},
            {"id": 9, "name": "Quiz 1", "modname": "quiz", "instance": 1009, "visible": 1},
        ],
    }
    assert [s["section"] for s in result] == [0, 1, 2]
    assert result[2]["modules"] == []


def test_outline_is_much_smaller_than_full(fake_call):
    outline = json.dumps(run(fake_call))
    full = json.dumps(run(fake_call, full=True))
    assert len(outline) * 4 < len(full)


def test_full_returns_moodle_data_unchanged(fake_call):
    assert run(fake_call, full=True) == COURSE


def test_sections_filter_applies_to_outline_and_full(fake_call):
    assert [s["section"] for s in run(fake_call, sections=[2, 0])] == [0, 2]
    assert run(fake_call, sections=[1], full=True) == [COURSE[1]]


def test_unknown_sections_give_an_empty_list(fake_call):
    assert run(fake_call, sections=[42]) == []


def test_every_tool_spells_the_course_parameter_courseid():
    # Models copy parameter names between tools; one tool taking course_id
    # while the rest take courseid made them send the wrong one.
    tools = asyncio.run(server.mcp.list_tools())
    names = {p for t in tools for p in t.inputSchema.get("properties", {}) if "course" in p}
    assert names <= {"courseid", "courseids"}, names


def test_outline_keeps_the_fields_other_code_reads(fake_call):
    # tests/integration/test_live.py reads section["section"] and each
    # module's id, name and modname.
    for s in run(fake_call):
        assert {"section", "modules"} <= set(s)
        for m in s["modules"]:
            assert {"id", "name", "modname"} <= set(m)
