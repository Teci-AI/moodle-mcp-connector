"""The README tool tables stay in sync with the registered tools."""

import ast
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


def _functions_reached(tool_names: set[str]) -> set[str]:
    """Every _call("...") wsfunction the given tools reach, through helpers."""
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    reached: set[str] = set()
    seen: set[str] = set()
    todo = list(tool_names)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "_call" and isinstance(node.args[0], ast.Constant):
                reached.add(node.args[0].value)
            elif node.func.id in funcs:
                todo.append(node.func.id)
    return reached


def test_ready_made_service_list_matches_content_read_tools():
    block = README.split("**MCP Connector\nService**", 1)[1].split("```", 2)[1]
    listed = set(block.split())
    core = {f for f in _functions_reached(server._CONTENT_READ_TOOLS)
            if not f.startswith(server.PLUGIN_PREFIX)}
    assert len(listed) == 8
    assert listed == core, listed ^ core


def test_plugin_functions_listed_match_the_code():
    source = Path(server.__file__).read_text(encoding="utf-8")
    in_code = set(re.findall(r'"(local_mcpconnector_\w+)"', source)) | {
        server.PLUGIN_PREFIX + n for n in re.findall(r'PLUGIN_PREFIX \+ "(\w+)"', source)
    }
    in_readme = set(re.findall(r"`(local_mcpconnector_\w+)`", README))
    assert in_readme == in_code, in_readme ^ in_code
