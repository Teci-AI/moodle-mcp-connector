"""The README tool tables stay in sync with the registered tools."""

import ast
import asyncio
import json
import re
import tomllib
from pathlib import Path

from moodle_mcp_connector import server

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
ROWS = re.findall(r"^\| `(\w+)` \| ?(✓?) ?\| (.*) \|$", README, re.M)


VERSION = tomllib.loads((Path(server.__file__).resolve().parent.parent / "pyproject.toml")
                        .read_text(encoding="utf-8"))["project"]["version"]
REPO = "github.com/Teci-AI/moodle-mcp-connector.git"


def readme_section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README, re.M | re.S)
    assert match, f"README has no '## {title}' section"
    return match.group(1)


def test_installation_leads_with_pip_and_uvx_from_the_release_tag():
    install = readme_section("Installation")
    pip_cmd = f'pip install "git+https://{REPO}@v{VERSION}"'
    uvx_cmd = f'uvx --from "git+https://{REPO}@v{VERSION}" moodle-mcp-connector'
    assert pip_cmd in install and uvx_cmd in install
    assert install.index(pip_cmd) < install.index(uvx_cmd)
    first_command = re.search(r"```bash\n(.*?)\n", install).group(1)
    assert first_command == pip_cmd


def test_private_repo_access_is_explained():
    install = readme_section("Installation")
    assert "While the repository is private" in install
    assert "credential helper" in install
    assert f"git+ssh://git@{REPO}@v{VERSION}" in install


def test_every_git_url_is_pinned_to_the_current_release():
    urls = re.findall(r"git\+(?:https|ssh)://[^\s\"']+", README)
    assert urls
    assert [u for u in urls if not u.endswith(f".git@v{VERSION}")] == []


def test_development_section_keeps_editable_install():
    dev = readme_section("Development")
    assert 'pip install -e ".[dev]"' in dev
    assert 'pip install -e ".[dev]"' not in readme_section("Installation")


def test_installation_states_the_plugin_version():
    install = readme_section("Installation")
    assert f"version {server.MIN_PLUGIN_VERSION}" in install


def test_json_config_examples_are_valid():
    blocks = re.findall(r"```json\n(.*?)```", README, re.S)
    assert len(blocks) >= 2
    for block in blocks:
        config = json.loads(block)
        command = config["mcp"]["moodle"]["command"]
        assert "moodle-mcp-connector" in command
        assert config["mcp"]["moodle"]["environment"]["MOODLE_TOKEN"] == "{env:MOODLE_TOKEN}"


PLUGIN_REPO_URL = "https://github.com/Teci-AI/moodle-local_mcpconnector"


def test_every_plugin_repository_mention_is_a_link():
    """Readers should reach the companion plugin in one click."""
    linked = f"[moodle-local_mcpconnector]({PLUGIN_REPO_URL})"
    assert linked in README
    without_links = README.replace(linked, "").replace(PLUGIN_REPO_URL, "")
    assert "moodle-local_mcpconnector" not in without_links


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
