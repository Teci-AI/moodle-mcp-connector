"""File downloads only ever send the token to the MOODLE_URL site."""

import asyncio
import base64
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from moodle_mcp_connector import server

TOKEN = "SECRET-TEST-TOKEN"  # set in conftest.py
FILE_PATH = "/webservice/pluginfile.php/12/mod_resource/content/1/notes.pdf"


def call_tool(name: str, args: dict):
    return asyncio.run(server.mcp.call_tool(name, args))


def tool_json(result) -> dict:
    """Decode a tool's JSON reply. Newer mcp versions return (content, structured)."""
    if isinstance(result, tuple):
        result = result[0]
    return json.loads(result[0].text)


@pytest.mark.parametrize("url", [
    "https://evil.example/steal",
    "https://moodle.test.evil.example/webservice/pluginfile.php/1/x",  # lookalike host
    "https://moodle.test@evil.example/webservice/pluginfile.php/1/x",  # userinfo trick
    "https://moodle.test:8443/webservice/pluginfile.php/1/x",          # other port
    "http://moodle.test/webservice/pluginfile.php/1/x",                # scheme downgrade
    "https://evil.example/?next=https://moodle.test/",
    "//moodle.test/webservice/pluginfile.php/1/x",
    "https://moodle.test",                                             # no path at all
    "file:///etc/passwd",
])
def test_download_file_refuses_urls_outside_moodle(fake_http, url):
    fake = fake_http({})
    with pytest.raises(ToolError) as exc:
        call_tool("download_file", {"fileurl": url})
    assert "Refusing to send the token" in str(exc.value)
    assert fake.requests == []  # nothing was sent anywhere


def test_download_file_fetches_moodle_file_with_token(fake_http):
    fake = fake_http({}, files={FILE_PATH: b"%PDF-1.7 data"})
    result = tool_json(call_tool(
        "download_file", {"fileurl": f"https://moodle.test{FILE_PATH}?forcedownload=1"}))
    assert base64.b64decode(result["content_base64"]) == b"%PDF-1.7 data"
    assert result["size"] == len(b"%PDF-1.7 data")
    [request] = fake.requests
    assert request.url.host == "moodle.test"
    assert request.url.params["token"] == TOKEN
    assert request.url.params["forcedownload"] == "1"


def test_download_errors_do_not_leak_the_token(fake_http):
    fake_http({}, files={})  # every download is a 404
    with pytest.raises(ToolError) as exc:
        call_tool("download_file", {"fileurl": f"https://moodle.test{FILE_PATH}"})
    assert "HTTP 404" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_read_activity_refuses_foreign_file_urls(fake_call, fake_http):
    fake = fake_http({})
    fake_call({"core_course_get_contents": [{"modules": [{
        "id": 5, "modname": "page", "name": "P",
        "contents": [{"type": "file", "filename": "index.html", "filepath": "/",
                      "fileurl": "https://evil.example/index.html"}],
    }]}]})
    with pytest.raises(server.MoodleError, match="Refusing to send the token"):
        asyncio.run(server.read_activity(2, 5))
    assert fake.requests == []


def test_read_activity_page_text(fake_call, fake_http):
    path = "/webservice/pluginfile.php/30/mod_page/content/index.html"
    fake_http({}, files={path: b"<h1>Title</h1><p>Hello&nbsp;world</p><script>x()</script>"})
    fake_call({"core_course_get_contents": [{"modules": [{
        "id": 5, "modname": "page", "name": "P",
        "contents": [{"type": "file", "filename": "index.html", "filepath": "/",
                      "fileurl": f"https://moodle.test{path}"}],
    }]}]})
    result = asyncio.run(server.read_activity(2, 5))
    assert result["text"] == "Title\n\nHello world"
