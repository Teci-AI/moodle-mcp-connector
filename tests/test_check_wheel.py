"""The wheel checker's content rules (scripts/check_wheel.py), on synthetic wheels.

The full check (clean venv install and MCP session) runs in CI's build job.
"""

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_wheel.py"
spec = importlib.util.spec_from_file_location("check_wheel", SCRIPT)
check_wheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_wheel)

METADATA = "Metadata-Version: 2.4\nName: moodle-mcp-connector\nVersion: 9.8.7\nLicense-Expression: MIT\n"
GOOD = {
    "moodle_mcp_connector/__init__.py": "",
    "moodle_mcp_connector/__main__.py": "",
    "moodle_mcp_connector/server.py": "",
    "moodle_mcp_connector-9.8.7.dist-info/METADATA": METADATA,
}


def make_wheel(tmp_path: Path, files: dict) -> Path:
    path = tmp_path / "moodle_mcp_connector-9.8.7-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def test_good_wheel_reports_its_version(tmp_path):
    assert check_wheel.check_contents(make_wheel(tmp_path, GOOD)) == "9.8.7"


@pytest.mark.parametrize("change", [
    {"tests/test_server.py": ""},                          # tests shipped
    {"scripts/check_wheel.py": ""},                        # tooling shipped
    {"moodle_mcp_connector/server.py": None},              # module missing
    {"moodle_mcp_connector-9.8.7.dist-info/METADATA": METADATA.replace("License-Expression: MIT\n", "")},
])
def test_bad_wheels_fail(tmp_path, change):
    files = {**GOOD, **change}
    files = {k: v for k, v in files.items() if v is not None}
    with pytest.raises(SystemExit) as exc:
        check_wheel.check_contents(make_wheel(tmp_path, files))
    assert exc.value.code == 1


def test_exactly_one_wheel_required(tmp_path):
    with pytest.raises(SystemExit):
        check_wheel.find_wheel(tmp_path)


def test_clean_env_drops_moodle_settings_and_pythonpath(monkeypatch):
    monkeypatch.setenv("MOODLE_TOKEN", "secret")
    monkeypatch.setenv("PYTHONPATH", "/src")
    env = check_wheel.clean_env(MOODLE_URL="http://x")
    assert "MOODLE_TOKEN" not in env and "PYTHONPATH" not in env
    assert env["MOODLE_URL"] == "http://x"
