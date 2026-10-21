# moodle-mcp-connector

An [MCP](https://modelcontextprotocol.io) server that connects AI agents to a
Moodle site through Moodle's REST web services.

It exposes Moodle as typed tools an agent can call: list and search courses,
read course structure, read the full text of Pages, Books, Labels and Lessons,
look at quizzes, grades, deadlines and completion, and, when explicitly
enabled, create and edit course content.

- **Read-only by default.** Write tools are only registered when
  `MOODLE_ALLOW_WRITE=true`.
- **Content tool set.** `MOODLE_TOOLSET=content` reduces the server to the
  tools needed to read and author course content.
- **Token stays private.** The token is read from the environment and is never
  logged or returned by any tool.

> This project is not affiliated with, endorsed by, or supported by Moodle HQ
> or Moodle Pty Ltd. Moodle™ is a registered trademark of Moodle Pty Ltd.

It is based on the MIT-licensed moodle-mcp-bridge project (version 1.7.0);
see [LICENSE](LICENSE).

## Requirements

- Python 3.11 or newer
- A Moodle site with web services enabled and the REST protocol turned on
- A web service token for a dedicated Moodle account (see [Security](#security))
- For the write tools that create and edit pages, books, labels, URLs,
  sections and activities: the companion Moodle plugin
  **local_mcpconnector** (repository: moodle-local_mcpconnector), version
  2026102123 or later, which has been tested on Moodle 5.3.
  `server_info` reports the installed plugin version and whether it is
  compatible. Read tools and the core-Moodle write tools (courses, users,
  enrolments, grading and so on) work without it.

## Installation

Clone the repository, then install it into a virtual environment:

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e .
```

This installs the `moodle-mcp-connector` command. Both of these start the
server:

```bash
moodle-mcp-connector
python -m moodle_mcp_connector
```

The server talks MCP over stdio. Started by hand, it waits silently for an MCP
client; that is expected. Stop it with Ctrl+C.

## Configuration

All configuration comes from environment variables. The server also loads a
`.env` file if one exists next to the package, in the repository root, or in
the working directory; variables already set in the environment take
precedence.

| Variable | Required | Description |
|---|---|---|
| `MOODLE_URL` | yes | Base URL of the site, e.g. `https://moodle.example.edu` (no trailing slash needed). |
| `MOODLE_TOKEN` | yes | Web service token. Never commit it. |
| `MOODLE_ALLOW_WRITE` | no | `true` registers the write tools. Anything else, or unset, means read-only. |
| `MOODLE_TOOLSET` | no | `content` registers only the content-focused tools (marked in the [tool table](#tools)). Unset or `all` registers every tool. Any other value stops the server with an error. |

Example `.env` (already listed in `.gitignore`):

```dotenv
MOODLE_URL=https://moodle.example.edu
MOODLE_TOKEN=replace-me
MOODLE_ALLOW_WRITE=false
MOODLE_TOOLSET=content
```

### Setting up Moodle

First, on any site:

1. *Site administration → General → Advanced features*: enable web services.
2. *Site administration → Server → Web services → Manage protocols*: enable REST.
3. Create a dedicated user for the agent (see [Security](#security)). Give it
   only the roles it needs, in the courses it should work on, and make sure
   it has the `webservice/rest:use` capability.

#### Content tool set: use the plugin's ready-made service

The local_mcpconnector plugin ships a web service called **MCP Connector
Service** (short name `mcpconnector`). It contains every `local_mcpconnector_*`
function plus the 8 core read functions the content tool set uses, with file
downloads allowed:

```
core_webservice_get_site_info
core_course_get_courses
core_course_get_courses_by_field
core_course_get_contents
core_enrol_get_users_courses
mod_quiz_get_quizzes_by_courses
mod_lesson_get_lessons_by_courses
mod_lesson_get_pages
```

It is disabled by default and limited to authorised users. To use it:

4. Install the plugin (*Site administration → Plugins → Install plugins*, or
   unpack it into `local/mcpconnector`) and complete the upgrade.
5. *Site administration → Server → Web services → External services*: edit
   **MCP Connector Service**, tick **Enabled**, and save.
6. On the same page, open **Authorised users** for that service and add the
   dedicated user.
7. *Site administration → Server → Web services → Manage tokens*: create a
   token for the dedicated user and **MCP Connector Service**. Put it in
   `MOODLE_TOKEN`, and set `MOODLE_TOOLSET=content`.

#### Other tools: use a custom service

The tools outside the content set (users, enrolments, grades, course
creation, assignments, forums, messages and so on) call core functions that
the ready-made service does not include. To use them, create your own
service instead:

4. *Site administration → Server → Web services → External services*: add a
   custom service, add only the functions your tools need (see the
   [tool tables](#tools)), and authorise the dedicated user. If you also want
   the plugin's write tools, add the `local_mcpconnector_*` functions you
   need.
5. Tick **Can download files** on the service if you use `read_activity` or
   `download_file`.
6. *Manage tokens*: create a token for the dedicated user and that service.

A token belongs to exactly one service, so a token for the ready-made service
can only reach the content tools, whatever `MOODLE_TOOLSET` says.

## OpenCode configuration

Add the server to `opencode.json` (project) or `~/.config/opencode/opencode.json`
(global). `{env:MOODLE_TOKEN}` reads the token from your shell environment so it
never lands in the config file:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "moodle": {
      "type": "local",
      "command": ["moodle-mcp-connector"],
      "enabled": true,
      "environment": {
        "MOODLE_URL": "https://moodle.example.edu",
        "MOODLE_TOKEN": "{env:MOODLE_TOKEN}",
        "MOODLE_TOOLSET": "content",
        "MOODLE_ALLOW_WRITE": "false"
      }
    }
  }
}
```

If `moodle-mcp-connector` is not on OpenCode's `PATH` (for example because it
lives in a virtual environment), use the full path to the executable, such as
`["C:/path/to/repo/.venv/Scripts/moodle-mcp-connector.exe"]` or
`["/path/to/repo/.venv/bin/moodle-mcp-connector"]`.

## Tools

The **Content** column marks the tools registered when `MOODLE_TOOLSET=content`.
Write tools are only registered when `MOODLE_ALLOW_WRITE=true`, in either tool
set.

Many read tools work on "the current user", meaning the user the token belongs
to. They look up that user's id once with `core_webservice_get_site_info` and
cache it. When an optional `courseid`/`courseids` argument is omitted, they use
`core_enrol_get_users_courses` to find that user's courses.

### Read tools

| Tool | Content | Moodle web service functions |
|---|:---:|---|
| `server_info` | ✓ | `core_webservice_get_site_info`; when plugin functions are present also `local_mcpconnector_get_info` |
| `verify_connection` | ✓ | `core_webservice_get_site_info` |
| `list_courses` | ✓ | `core_course_get_courses` |
| `search_courses` | ✓ | `core_course_get_courses_by_field` |
| `get_my_courses` | ✓ | `core_webservice_get_site_info`, `core_enrol_get_users_courses` |
| `get_course_content` | ✓ | `core_course_get_contents` |
| `list_quizzes` | ✓ | `mod_quiz_get_quizzes_by_courses` |
| `search_course_materials` | ✓ | `core_course_get_contents`; without `courseid` also `core_webservice_get_site_info`, `core_enrol_get_users_courses` |
| `read_activity` | ✓ | `core_course_get_contents`, then downloads the activity's `.html` files from `webservice/pluginfile.php` (needs **Can download files**) |
| `read_lesson` | ✓ | `mod_lesson_get_lessons_by_courses`, `mod_lesson_get_pages` |
| `get_enrolled_users` | | `core_enrol_get_enrolled_users` |
| `get_user_grades` | | `gradereport_user_get_grade_items` |
| `get_course_announcements` | | `mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions`; without `courseid` also `core_webservice_get_site_info`, `core_enrol_get_users_courses` |
| `get_recent_activity` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions` |
| `get_assignments` | | `mod_assign_get_assignments`; without `courseids` also `core_webservice_get_site_info`, `core_enrol_get_users_courses` |
| `get_assignment_status` | | `core_webservice_get_site_info`, `mod_assign_get_submission_status` |
| `get_upcoming_deadlines` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status` |
| `get_overdue_assignments` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status` |
| `get_actionable_tasks` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status` |
| `analyze_assignment` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `core_course_get_contents` |
| `extract_assignment_requirements` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments` |
| `find_relevant_materials` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `core_course_get_contents` |
| `decompose_task` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `core_course_get_contents` |
| `create_implementation_plan` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `core_course_get_contents` |
| `get_grades` | | without `courseid`: `gradereport_overview_get_course_grades`; with `courseid`: `core_webservice_get_site_info`, `gradereport_user_get_grade_items` |
| `get_course_progress` | | `core_webservice_get_site_info`, `core_completion_get_activities_completion_status`; without `courseid` also `core_enrol_get_users_courses` |
| `get_course_health` | | `core_webservice_get_site_info`, `core_completion_get_activities_completion_status`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `gradereport_overview_get_course_grades` |
| `get_study_load` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status` |
| `get_upcoming_events` | | `core_calendar_get_action_events_by_timesort` |
| `semester_dashboard` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `gradereport_overview_get_course_grades` |
| `daily_briefing` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `core_calendar_get_action_events_by_timesort`, `gradereport_overview_get_course_grades` |
| `weekly_review` | | `core_webservice_get_site_info`, `core_enrol_get_users_courses`, `mod_assign_get_assignments`, `mod_assign_get_submission_status`, `core_completion_get_activities_completion_status` |
| `ask_moodle` | | Routes the question to one of `get_overdue_assignments`, `get_upcoming_deadlines`, `get_grades` (overview), `get_course_progress`, `get_course_announcements`, `get_upcoming_events`, `get_actionable_tasks`, `get_my_courses` or `daily_briefing`, and calls that tool's functions |
| `get_quiz_results` | | `mod_quiz_get_user_attempts`, `mod_quiz_get_user_best_grade` |
| `get_quiz_attempt_review` | | `mod_quiz_get_attempt_review` |
| `get_quiz_report` | | `core_enrol_get_enrolled_users`, `mod_quiz_get_user_best_grade`, `mod_quiz_get_user_attempts`, `mod_quiz_get_attempt_review` |
| `get_activity_completion` | | `core_completion_get_activities_completion_status`; with `userid` 0 also `core_webservice_get_site_info` |
| `get_course_completion_status` | | `core_completion_get_course_completion_status`; with `userid` 0 also `core_webservice_get_site_info` |
| `get_notifications` | | `core_webservice_get_site_info`, `message_popup_get_unread_popup_notification_count`, `message_popup_get_popup_notifications` |
| `list_course_competencies` | | `core_competency_list_course_competencies` |
| `get_learning_plans` | | `core_competency_list_user_plans`; with `userid` 0 also `core_webservice_get_site_info` |

### Write tools (`MOODLE_ALLOW_WRITE=true`)

Tools marked **plugin** need the companion plugin
[moodle-local_mcpconnector](#requirements), which provides the
`local_mcpconnector_*` functions.

Plugin version 2026102123 provides every function below. If the site runs an
older plugin, or the token's service leaves a function out, the tool fails
with a message naming the missing function rather than a raw Moodle error,
and `build_course` checks every function an outline needs before it writes
anything, rejecting the outline if any is missing.

| Tool | Content | Moodle web service functions |
|---|:---:|---|
| `create_page` | ✓ | `local_mcpconnector_create_page` (plugin) |
| `update_page` | ✓ | `local_mcpconnector_update_page` (plugin) |
| `create_book` | ✓ | `local_mcpconnector_create_book` (plugin) |
| `create_label` | ✓ | `local_mcpconnector_create_label` (plugin) |
| `create_url` | ✓ | `local_mcpconnector_create_url` (plugin) |
| `create_quiz` | ✓ | `local_mcpconnector_create_quiz` (plugin) |
| `add_quiz_question` | ✓ | `local_mcpconnector_add_quiz_question` (plugin) |
| `add_truefalse_question` | ✓ | `local_mcpconnector_add_truefalse_question` (plugin) |
| `add_shortanswer_question` | ✓ | `local_mcpconnector_add_shortanswer_question` (plugin) |
| `add_essay_question` | ✓ | `local_mcpconnector_add_essay_question` (plugin) |
| `add_numerical_question` | ✓ | `local_mcpconnector_add_numerical_question` (plugin) |
| `add_matching_question` | ✓ | `local_mcpconnector_add_matching_question` (plugin) |
| `create_section` | ✓ | `local_mcpconnector_create_section` (plugin) |
| `delete_section` | ✓ | `local_mcpconnector_delete_section` (plugin) |
| `update_activity` | ✓ | `local_mcpconnector_update_activity` (plugin) |
| `move_activity` | ✓ | `local_mcpconnector_move_activity` (plugin) |
| `delete_activity` | ✓ | `local_mcpconnector_delete_activity` (plugin) |
| `create_forum` | | `local_mcpconnector_create_forum` (plugin) |
| `create_choice` | | `local_mcpconnector_create_choice` (plugin) |
| `create_assignment` | | `local_mcpconnector_create_assignment` (plugin) |
| `backup_course` | | `local_mcpconnector_backup_course` (plugin) |
| `restore_course` | | `local_mcpconnector_restore_course` (plugin) |
| `build_course` | | `core_webservice_get_site_info` (to check every needed function first), `core_course_create_courses`, `local_mcpconnector_create_section`, `local_mcpconnector_create_page`, `local_mcpconnector_create_label`, `local_mcpconnector_create_url`, `local_mcpconnector_create_forum`, `local_mcpconnector_create_assignment`, `local_mcpconnector_create_choice`, `local_mcpconnector_create_quiz`, the six `local_mcpconnector_add_*_question` functions (plugin); with `rollback=True` also `core_course_delete_courses` |
| `create_course` | | `core_course_create_courses` |
| `update_course` | | `core_course_update_courses` |
| `delete_courses` | | `core_course_delete_courses` |
| `import_course` | | `core_course_import_course` |
| `create_category` | | `core_course_create_categories` |
| `delete_category` | | `core_course_delete_categories` |
| `create_user` | | `core_user_create_users` |
| `update_user` | | `core_user_update_users` |
| `delete_users` | | `core_user_delete_users` |
| `enrol_user` | | `enrol_manual_enrol_users` |
| `enrol_users` | | `enrol_manual_enrol_users` |
| `unenrol_user` | | `enrol_manual_unenrol_users` |
| `assign_role` | | `core_role_assign_roles` |
| `unassign_role` | | `core_role_unassign_roles` |
| `create_group` | | `core_group_create_groups` |
| `add_group_members` | | `core_group_add_group_members` |
| `create_cohort` | | `core_cohort_create_cohorts` |
| `add_cohort_members` | | `core_cohort_add_cohort_members` |
| `grade_assignment` | | `mod_assign_save_grade` |
| `start_forum_discussion` | | `mod_forum_add_discussion` |
| `reply_forum_post` | | `mod_forum_add_discussion_post` |
| `send_message` | | `core_message_send_instant_messages` |
| `mark_activity_complete` | | `core_completion_update_activity_completion_status_manually` |
| `override_activity_completion` | | `core_completion_override_activity_completion_status` |
| `upload_file` | | `core_files_upload` |
| `download_file` | | None; downloads a file from `webservice/pluginfile.php` (needs **Can download files**) |

## Security

- **Use a dedicated Moodle account** for the agent, never a site administrator
  or your personal login. Give it only the roles it needs, and only in the
  courses it should touch.
- **Give the web service the minimum functions.** Add only the functions in
  the table above that your enabled tools actually call. A tool whose function
  is missing fails with a clear Moodle error; it cannot reach anything the
  service does not allow.
- **Keep write mode off unless you need it.** Without `MOODLE_ALLOW_WRITE=true`
  no write tool is registered at all. Combine write mode with
  `MOODLE_TOOLSET=content` to limit an authoring agent to course content.
- **Never commit tokens.** Keep `MOODLE_TOKEN` in your environment or in a
  `.env` file (ignored by git). In client configs, reference the environment
  (for example `{env:MOODLE_TOKEN}` in OpenCode) rather than pasting the
  token. If a token leaks, revoke it in *Manage tokens* and issue a new one.
- The token is sent as a POST field for web service calls. File downloads
  (`read_activity`, `download_file`) have to put it in the URL, so request
  URLs are kept out of logs and error messages, and both tools refuse any URL
  outside the `MOODLE_URL` site. An agent cannot get the token sent to
  another host.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a new tool.

## License

MIT; see [LICENSE](LICENSE).
