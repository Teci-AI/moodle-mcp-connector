# Changes

## 0.3.1 (2026-11-05)

Documentation only: the README shows how to connect any MCP client, not just
OpenCode.

- **New section "Connecting an MCP client"**, replacing "OpenCode
  configuration": the command and environment every client needs, then
  ready-to-copy configurations for Claude Code, Claude Desktop and OpenCode,
  and where other clients take the same settings.
- The OpenCode example now runs the server with `uvx moodle-mcp-connector`,
  as the other examples do.
- How to fix `uvx` failing with `invalid peer certificate: UnknownIssuer` on
  networks that inspect HTTPS traffic (`--system-certs`).

What it contains, unchanged from 0.3.0: **90 tools**, 41 read tools and 49
write tools; `MOODLE_ALLOW_WRITE=true` enables the write tools, and
`MOODLE_TOOLSET=content` limits the server to 10 read tools, plus 17 write
tools when writes are enabled.

Requirements: Python 3.11 or newer, and for the plugin write tools
**local_mcp_connector 2.0.0 or newer (version 2026110500)**, from the
[moodle-local_mcp_connector releases](https://github.com/Teci-AI/moodle-local_mcp_connector/releases).

## 0.3.0 (2026-11-05)

Follows the companion plugin's new component name. **Breaking:** this
release only works with the renamed plugin.

- The companion plugin MCP Connector is now component `local_mcp_connector`
  (it was `local_mcpconnector` up to its 1.0.0), because another Moodle
  plugin already uses the old name. The plugin write tools call
  `local_mcp_connector_*` functions, and the ready-made service "MCP
  Connector Service" has the short name `mcp_connector`. Install the renamed plugin, authorise the dedicated
  user on its service and create a token for it; then remove
  `local_mcpconnector`.
- `server_info` reports the plugin as compatible from its release 2.0.0.

What it contains, unchanged from 0.2.0: **90 tools**, 41 read tools and 49
write tools; `MOODLE_ALLOW_WRITE=true` enables the write tools, and
`MOODLE_TOOLSET=content` limits the server to 10 read tools, plus 17 write
tools when writes are enabled. Parameters are unchanged too.

Requirements: Python 3.11 or newer, and for the plugin write tools
**local_mcp_connector 2.0.0 or newer (version 2026110500)**, from the
[moodle-local_mcp_connector releases](https://github.com/Teci-AI/moodle-local_mcp_connector/releases).

## 0.2.0 (2026-11-04)

Smaller course structures for MCP clients, and one consistent name for the
course parameter. Both changes can break existing callers; see below.

- **`get_course_content` returns a compact outline by default.** For each
  section: its number (`section`), `id`, `name` and `visible`, and its
  `modules` with `id` (the cmid), `name`, `modname`, `instance` and
  `visible`. Names are no longer HTML-escaped (`&` instead of `&amp;`).
  Moodle's full reply for a 30-section course is over 100 KB, which MCP
  clients such as OpenCode cut before the model sees it; the outline of the
  same course is about 13 KB. **Breaking:** pass `full=True` to get Moodle's
  complete data, as 0.1.x returned it.
- **`get_course_content` can return only some sections:** `sections=[12, 13]`
  limits the reply to those section numbers, in outline or full form.
- **`read_activity` and `read_lesson` take `courseid`**, like every other
  tool, instead of `course_id`; their results use `courseid` too. Models copy
  parameter names between tools, and the one-off spelling made them send the
  wrong one. **Breaking:** callers passing `course_id` must rename it.

What it contains, as in 0.1.1: **90 tools**, 41 read tools and 49 write
tools; `MOODLE_ALLOW_WRITE=true` enables the write tools, and
`MOODLE_TOOLSET=content` limits the server to 10 read tools, plus 17 write
tools when writes are enabled.

Requirements: Python 3.11 or newer, and for the plugin write tools
**local_mcpconnector 0.1.0 or newer (version 2026102123)**, from the
[moodle-local_mcpconnector releases](https://github.com/Teci-AI/moodle-local_mcpconnector/releases).

## 0.1.1 (2026-10-23)

First release on PyPI. Install it with `pip install moodle-mcp-connector`, or
run it with `uvx moodle-mcp-connector`. The tools are unchanged since 0.1.0.

- **Install from PyPI.** The
  [README](https://github.com/Teci-AI/moodle-mcp-connector/blob/main/README.md)
  now leads with the PyPI install and uses it in the OpenCode examples.
  Installing from a GitHub release tag still works.
- **The README works on PyPI.** Its links are absolute, so they work on the
  PyPI project page as well as on GitHub, and every mention of the companion
  plugin links to its repository.
- **Releases are published by CI** with PyPI Trusted Publishing, after the
  tests, the build checks and a smoke test of the wheel in a clean virtual
  environment.

What it contains, as in 0.1.0: **90 tools**, 41 read tools and 49 write
tools; `MOODLE_ALLOW_WRITE=true` enables the write tools, and
`MOODLE_TOOLSET=content` limits the server to 10 read tools, plus 17 write
tools when writes are enabled.

Requirements: Python 3.11 or newer, and for the plugin write tools
**local_mcpconnector 0.1.0 or newer (version 2026102123)**, from the
[moodle-local_mcpconnector releases](https://github.com/Teci-AI/moodle-local_mcpconnector/releases).

## 0.1.0 (2026-10-22)

First release of moodle-mcp-connector, an MCP server that connects AI agents
to Moodle through its REST web services. It is based on the MIT-licensed
moodle-mcp-bridge 1.7.0.

### Tools

- **90 tools**: 41 read tools, always available, and 49 write tools. The
  [README](https://github.com/Teci-AI/moodle-mcp-connector/blob/main/README.md#tools)
  lists every tool with the exact Moodle web
  service functions it calls.
- **Reading course content**: `read_activity` returns the full plain text of a
  Page, Book (every chapter) or Label; `read_lesson` returns a Lesson's pages
  in order. Other read tools cover courses, course structure, quizzes and
  quiz results, assignments and deadlines, grades, completion, notifications
  and competencies.
- **Authoring course content** with the companion plugin: create and edit
  pages, books, labels, URLs, sections and activities; create quizzes and add
  multiple-choice, true/false, short-answer, essay, numerical and matching
  questions; create forums, choices and assignments; back up and restore
  courses; and build a whole course from one outline with `build_course`,
  which checks the outline and the site's functions before writing anything
  and can roll back on failure.
- **Managing courses and users** with core Moodle functions: courses,
  categories, users, enrolments, groups, cohorts, roles, grading, forum
  posts, messages and completion.

### Switches

- **`MOODLE_ALLOW_WRITE=true`** registers the write tools. Without it the
  server is read-only and no write tool exists at all.
- **`MOODLE_TOOLSET=content`** limits the server to the tools needed to read
  and author course content: 10 read tools, plus 17 write tools when writes
  are enabled. Unset (or `all`) registers every tool; any other value stops
  the server with an error.

### Safety

- The token is read from the environment (or a `.env` file), is sent only to
  the `MOODLE_URL` site, and never appears in logs, errors or tool results.
- `server_info` reports the configuration, whether the site is reachable,
  and whether the installed plugin is compatible.
- Invalid input is rejected with Moodle's own reason ("Invalid parameter
  value: ..."), and lists that are too short are refused before anything is
  sent.

### Requirements

- Python 3.11 or newer.
- For the plugin write tools: the Moodle plugin **local_mcpconnector 0.1.0 or
  newer (version 2026102123)**, from the
  [moodle-local_mcpconnector releases](https://github.com/Teci-AI/moodle-local_mcpconnector/releases).
  Its ready-made "MCP Connector Service" covers the content tool set. Read
  tools and the core-Moodle write tools work without the plugin.
