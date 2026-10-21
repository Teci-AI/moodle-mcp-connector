"""backup_course saves backups under Moodle's own filename by default."""

import asyncio
import base64
import json
from pathlib import Path

import pytest

from moodle_mcp_connector import server

MOODLE_NAME = "backup-moodle2-course-12-bio101-20261021-1200-nu.mbz"
DATA = b"PK\x03\x04 fake mbz"


def backup_result(filename=MOODLE_NAME) -> dict:
    return {"filename": filename, "size": len(DATA),
            "content_base64": base64.b64encode(DATA).decode("ascii")}


def run_backup(**args) -> dict:
    result = asyncio.run(server.mcp.call_tool("backup_course", {"courseid": 12, **args}))
    if isinstance(result, tuple):  # newer mcp versions: (content, structured)
        result = result[0]
    return json.loads(result[0].text)


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_default_saves_under_moodle_filename(fake_call, in_tmp):
    fake = fake_call({"local_mcpconnector_backup_course": backup_result()})
    out = run_backup()
    saved = in_tmp / MOODLE_NAME
    assert saved.read_bytes() == DATA
    assert Path(out["saved_to"]) == saved.resolve()
    assert out["filename"] == MOODLE_NAME
    assert out["size"] == len(DATA)
    assert not (in_tmp / "course_12.mbz").exists()
    assert fake.calls == [("local_mcpconnector_backup_course",
                           {"courseid": 12, "includeusers": 0})]


def test_save_to_still_wins(fake_call, in_tmp):
    fake_call({"local_mcpconnector_backup_course": backup_result()})
    target = in_tmp / "mine.mbz"
    out = run_backup(save_to=str(target))
    assert target.read_bytes() == DATA
    assert Path(out["saved_to"]) == target.resolve()
    assert not (in_tmp / MOODLE_NAME).exists()


@pytest.mark.parametrize("hostile", [
    "../../evil.mbz", "..\\..\\evil.mbz", "/etc/evil.mbz", "C:\\Windows\\evil.mbz",
])
def test_server_filename_cannot_escape_current_directory(fake_call, in_tmp, hostile):
    fake_call({"local_mcpconnector_backup_course": backup_result(hostile)})
    out = run_backup()
    assert Path(out["saved_to"]) == (in_tmp / "evil.mbz").resolve()
    assert (in_tmp / "evil.mbz").read_bytes() == DATA


@pytest.mark.parametrize("unusable", ["", None, "..", "dir/"])
def test_unusable_filename_falls_back(fake_call, in_tmp, unusable):
    fake_call({"local_mcpconnector_backup_course": backup_result(unusable)})
    out = run_backup()
    assert Path(out["saved_to"]) == (in_tmp / "course_12.mbz").resolve()
