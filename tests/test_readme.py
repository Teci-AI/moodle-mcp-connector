"""The README tool tables stay in sync with the registered tools."""

import asyncio
import re
from pathlib import Path

from moodle_mcp_connector import server

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
ROWS = re.findall(r"^\| `(\w+)` \| ?(✓?) ?\| (.*) \|$", README, re.M)


def test_every_tool_has_exactly_one_row():
    names = [name for name, _, _ in ROWS]
    assert len(names) == len(set(names)), "duplicate rows"
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert set(names) == registered, set(names) ^ registered


def test_content_column_matches_content_toolset():
    marked = {name for name, mark, _ in ROWS if mark}
    assert marked == server._CONTENT_READ_TOOLS | server._CONTENT_WRITE_TOOLS


def test_plugin_functions_listed_match_the_code():
    source = Path(server.__file__).read_text(encoding="utf-8")
    in_code = set(re.findall(r'"(local_mcpconnector_\w+)"', source)) | {
        server.PLUGIN_PREFIX + n for n in re.findall(r'PLUGIN_PREFIX \+ "(\w+)"', source)
    }
    in_readme = set(re.findall(r"`(local_mcpconnector_\w+)`", README))
    assert in_readme == in_code, in_readme ^ in_code
