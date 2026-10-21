# Contributing

Thanks for helping out. This guide covers setting up a development copy and
adding a new tool.

## Development setup

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e .
```

Put a test site's details in a `.env` file in the repository root (it is
ignored by git). Use a test site and a test account, never production
credentials:

```dotenv
MOODLE_URL=https://moodle-test.example.edu
MOODLE_TOKEN=replace-me
```

## How tools are organised

Every tool lives in [`moodle_mcp_connector/server.py`](moodle_mcp_connector/server.py):

- `_call(wsfunction, params)` calls one Moodle web service function and
  returns the parsed JSON. It flattens nested dicts and lists into Moodle's
  `name[0][key]=value` form encoding and turns Moodle error payloads into a
  `MoodleError`. Always go through it, never call httpx directly for web
  service functions.
- `@tool()` registers a function as an MCP tool. Use it instead of
  `@mcp.tool()`: it applies the `MOODLE_TOOLSET` filter.
- **Read tools** are defined at module level and are always registered.
- **Write tools** are defined inside `_register_write_tools()`, which only
  runs when `MOODLE_ALLOW_WRITE=true`.
- `_CONTENT_READ_TOOLS` and `_CONTENT_WRITE_TOOLS` list the tools included in
  `MOODLE_TOOLSET=content`.

## Adding a tool

### 1. Find the Moodle function

Look the function up in your site's API documentation (*Site administration →
Development → API Documentation*) and check its parameters, return value and
the capabilities it requires. Prefer core Moodle functions. A new function in
the companion plugin (moodle-local_mcpconnector) is only worth adding when
core has no way to do the job.

### 2. Write the function

Read tool, at module level next to related tools:

```python
@tool()
async def get_course_sections(courseid: int) -> list:
    """List the section names of a course, in order.

    Read-only. Returns each section's number and name.
    """
    contents = await _call("core_course_get_contents", {"courseid": courseid})
    return [{"section": s.get("section"), "name": s.get("name")} for s in contents]
```

Write tool, inside `_register_write_tools()` (indented one level):

```python
    @tool()
    async def rename_section(sectionid: int, name: str) -> dict:
        """⚠️ WRITES LIVE DATA. Rename a course section.

        Uses core_update_inplace_editable. Returns the updated value.
        """
        return await _call(
            "core_update_inplace_editable",
            {"component": "format_topics", "itemtype": "sectionname",
             "itemid": sectionid, "value": name},
        )
```

Conventions:

- **The docstring is what the agent reads.** The first line says what the tool
  does; then say what the arguments mean (especially ids: is it a `cmid` or an
  instance id?) and what comes back.
- Start read tool docstrings with `Read-only.` and write tool docstrings with
  `⚠️ WRITES LIVE DATA.`. Add `IRREVERSIBLE` for deletes.
- Use type hints on every argument. FastMCP builds the tool's input schema
  from them, and simple defaults (`0`, `""`, `-1` for "leave unchanged") keep
  the schema easy for agents.
- Return plain dicts and lists. Strip HTML from text an agent will read: use
  `_strip_html` for short fields and `_html_to_text` for whole documents.
- Raise `MoodleError` with a clear message for expected failures (not found,
  wrong activity type). Don't catch errors just to hide them.
- **Never put the token in a return value, error message or log line.** If a
  file URL needs `token=`, use `_download_text`, which only sends the token to
  `MOODLE_URL`.

### 3. Decide whether it belongs in the content tool set

If the tool reads or authors course content (pages, books, labels, URLs,
lessons, quizzes, questions, sections, activities), add its name to
`_CONTENT_READ_TOOLS` or `_CONTENT_WRITE_TOOLS`. Otherwise leave those sets
alone and the tool appears only in the full tool set.

### 4. Document it

Add a row to the right table in [README.md](README.md#tools) listing **every**
Moodle web service function the tool calls, including those reached through
helpers such as `_get_userid` (`core_webservice_get_site_info`) or
`_my_courses_raw` (`core_enrol_get_users_courses`). Operators rely on that
table to build a minimal web service, so a missing function shows up as a
"function not available" error for them.

### 5. Check it

Confirm the tool is registered in the modes you expect:

```bash
python -c "import asyncio; from moodle_mcp_connector.server import mcp; print(sorted(t.name for t in asyncio.run(mcp.list_tools())))"
```

Run that with `MOODLE_ALLOW_WRITE` and `MOODLE_TOOLSET` set to the
combinations that matter for your tool. Then call it against a test site
from an MCP client, or with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector moodle-mcp-connector
```

Try the error paths too: a wrong id, and a token whose service lacks the
function.

## Pull requests

- Keep each pull request to one change, with a commit message that says what
  changed and why.
- Mention which Moodle version you tested against.
- Never commit `.env` files, tokens or real site URLs.
