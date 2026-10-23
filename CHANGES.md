# Changes

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
