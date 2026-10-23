# Contributing

Thanks for helping out. This guide covers setting up a development copy and
adding a new tool.

## Development setup

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"
```

Run the tests with:

```bash
pytest
```

They need no Moodle site: `tests/conftest.py` sets a fake configuration and
the tests replace `_call` or the HTTP transport with fakes. To check against a
real site, put a test site's details in a `.env` file in the repository root (it is
ignored by git). Use a test site and a test account, never production
credentials:

```dotenv
MOODLE_URL=https://moodle-test.example.edu
MOODLE_TOKEN=replace-me
```

## Building the package

CI checks on every push that the package builds and installs. To run the
same check locally:

```bash
python -m build                      # wheel and sdist in dist/
python -m twine check --strict dist/*
python scripts/check_wheel.py dist   # install the wheel in a clean venv and smoke-test it
```

`check_wheel.py` installs the wheel into a fresh virtual environment outside
the repository, checks that `moodle-mcp-connector` and
`python -m moodle_mcp_connector` exit cleanly without any configuration, runs
a short MCP session, and fails if the token ever appears in the output. Add
`--installer uv` to install with uv instead of pip.

## Releasing

Releases are published by `.github/workflows/release.yml` with PyPI
Trusted Publishing, so no PyPI token is stored anywhere.

### One-time setup

1. On https://pypi.org and https://test.pypi.org: *Your account → Publishing →
   Add a new pending publisher* (it becomes a normal trusted publisher after
   the first upload), with these exact values:

   | Field | PyPI | TestPyPI |
   |---|---|---|
   | PyPI project name | `moodle-mcp-connector` | `moodle-mcp-connector` |
   | Owner | `Teci-AI` | `Teci-AI` |
   | Repository name | `moodle-mcp-connector` | `moodle-mcp-connector` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `testpypi` |

2. On GitHub, *Settings → Environments*: create `pypi`, with yourself as a
   **required reviewer** so every PyPI upload waits for approval, and
   `testpypi`.

### Dry run on TestPyPI

*Actions → Release → Run workflow* on `main`. It checks the version and its
CHANGES.md section, runs the tests, builds and smoke-tests the wheel, and
uploads it to TestPyPI. A version can be uploaded only once, so repeated dry
runs of the same version keep the files from the first one. Try it in a clean
virtual environment:

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple moodle-mcp-connector==X.Y.Z
```

### Release

1. Set the new version in `pyproject.toml` (the only place it is written).
2. Add a `## X.Y.Z (YYYY-MM-DD)` section to `CHANGES.md`. It becomes the
   GitHub release notes, so use absolute links.
3. Update the version in the README's pinned install commands; the tests fail
   until it matches.
4. Commit, then tag and push:

   ```bash
   git tag -a vX.Y.Z -m "moodle-mcp-connector X.Y.Z"
   git push origin main vX.Y.Z
   ```

5. Approve the `pypi` deployment in the workflow run. The workflow publishes to
   PyPI first and creates the GitHub release (with the wheel and sdist) only
   after that succeeds. Versions below 1.0.0 are marked as pre-releases on
   GitHub; on PyPI they are normal releases.

PyPI never lets a version be uploaded twice, even after deleting it. If a
release goes wrong, fix it and release the next patch version.

## Live integration tests

`tests/integration` runs every plugin tool against a real Moodle site,
through the same MCP layer an agent uses. The tests are opt-in: they skip
unless `MOODLE_TEST_URL` and `MOODLE_TEST_TOKEN` are set, and CI never sets
them.

Each run creates its own hidden throwaway course (shortname
`mcptest-<timestamp>-<random>-main`) in a test category, plus a few more
courses that `build_course` and `restore_course` create. At the end it
deletes all of them, even when tests fail. It never touches any other
course, and teardown refuses to delete a course whose shortname does not
start with `mcptest-`. **Use a development or test site, never production.**

### One-time setup on the Moodle site

1. Install **local_mcpconnector 0.1.0 (version 2026102123) or newer**.
2. Create an empty category for the tests, for example "MCP connector tests",
   and note its id (the `categoryid=` value in its URL).
3. Create a dedicated user for the tests, for example `mcp-tests`.
4. Give that user:
   - the **Manager** role in the test category (*category → Permissions →
     Assign roles*). Every throwaway course inherits it, so it covers
     creating, editing, backing up and deleting them. It also gives the
     restore capabilities (`moodle/course:create`,
     `moodle/restore:restorecourse`, `moodle/restore:uploadfile`) in the
     category, which a Teacher role does not have. A Teacher role in one fixed
     course is not enough, because each run works in courses it has just
     created.
   - the `webservice/rest:use` capability at system level, for example
     through a system role that has only that capability.
5. Create a **custom** web service for the tests (*Site administration →
   Server → Web services → External services → Add*): tick **Enabled**,
   **Authorised users only** and **Can download files**, then add exactly
   these functions:

   ```
   core_webservice_get_site_info
   core_course_create_courses
   core_course_delete_courses
   core_course_get_contents
   core_course_get_courses_by_field
   local_mcpconnector_get_info
   local_mcpconnector_create_section
   local_mcpconnector_delete_section
   local_mcpconnector_create_page
   local_mcpconnector_update_page
   local_mcpconnector_create_book
   local_mcpconnector_create_label
   local_mcpconnector_create_url
   local_mcpconnector_create_forum
   local_mcpconnector_create_choice
   local_mcpconnector_create_assignment
   local_mcpconnector_update_activity
   local_mcpconnector_move_activity
   local_mcpconnector_delete_activity
   local_mcpconnector_create_quiz
   local_mcpconnector_add_quiz_question
   local_mcpconnector_add_truefalse_question
   local_mcpconnector_add_shortanswer_question
   local_mcpconnector_add_essay_question
   local_mcpconnector_add_numerical_question
   local_mcpconnector_add_matching_question
   local_mcpconnector_backup_course
   local_mcpconnector_restore_course
   ```

   The plugin's ready-made **MCP Connector Service** is not enough on its
   own: it has every plugin function but not `core_course_create_courses` or
   `core_course_delete_courses`, which the tests need for their throwaway
   courses and `build_course` needs for rollback. Moodle does not allow
   functions to be added to a plugin's built-in service.
6. Open **Authorised users** for the new service and add the test user.
7. *Manage tokens*: create a token for the test user and the new service.

### Configure and run

Put the settings in `.env.test` at the repository root (git-ignored), or set
them as environment variables, which take precedence:

```dotenv
MOODLE_TEST_URL=https://moodle-dev.example.edu
MOODLE_TEST_TOKEN=paste-the-test-token-here
MOODLE_TEST_CATEGORYID=12
```

Then:

```bash
pip install -e ".[dev]"
pytest tests/integration -v
```

There are 16 tests, which take a minute or two. Without
`MOODLE_TEST_CATEGORYID` only the `server_info` check runs. Nothing prints
the token: it is kept out of assertion messages, and every test report is
scrubbed of it.

If a run is killed before its teardown (for example the process is
terminated), courses named `mcptest-...` may be left in the test category;
delete them by hand. If the category recycle bin is enabled, deleted test
courses also show up there for a while, which is normal Moodle behaviour.

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
