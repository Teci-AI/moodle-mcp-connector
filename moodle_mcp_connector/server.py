"""moodle-mcp-connector — an MCP server that exposes a Moodle instance as tools.

Read tools are always registered. Write tools (course/user/activity creation,
enrolment, file upload) are only registered when MOODLE_ALLOW_WRITE=true, so a
misconfiguration can never silently expose destructive operations.

Config comes from environment variables only — the token is never logged.

    MOODLE_URL          e.g. https://moodle.example.edu
    MOODLE_TOKEN        a web service token
    MOODLE_ALLOW_WRITE  "true" to enable write tools (default: read-only)
    MOODLE_TOOLSET      "content" for the content-focused subset (default: all)

All calls hit:
    {MOODLE_URL}/webservice/rest/server.php
        ?wstoken=...&wsfunction=...&moodlewsrestformat=json
"""

from __future__ import annotations

import asyncio
import html as _htmllib
import importlib.metadata as _metadata
import json
import logging
import os
import re
import time as _time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Configuration (env vars only — never hard-code or log the token).
# Load a .env sitting next to this file so the server works regardless of the
# working directory it is launched from (e.g. by an MCP client). Real env vars
# already set take precedence over the file.
# --------------------------------------------------------------------------- #

try:
    from dotenv import load_dotenv

    # Look for a .env next to this module, in its parent (the repo checkout
    # layout), then the working directory. First value found wins.
    _here = Path(__file__).resolve().parent
    for _candidate in (_here / ".env", _here.parent / ".env", Path.cwd() / ".env"):
        if _candidate.is_file():
            load_dotenv(_candidate)
except ImportError:
    pass

MOODLE_URL = os.environ.get("MOODLE_URL", "").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "")
ALLOW_WRITE = os.environ.get("MOODLE_ALLOW_WRITE", "").strip().lower() == "true"

REST_ENDPOINT = f"{MOODLE_URL}/webservice/rest/server.php"

# Every function of the companion plugin, local_mcpconnector, starts with this.
PLUGIN_PREFIX = "local_mcpconnector_"

# httpx logs every request URL at INFO. File downloads carry the token in the
# query string, so keep httpx quiet to stop it reaching MCP client logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("moodle-mcp-connector")

# --------------------------------------------------------------------------- #
# Tool sets. MOODLE_TOOLSET=content registers only the tools needed to read and
# author course content; unset (or "all") registers everything. Write tools in
# either set still require MOODLE_ALLOW_WRITE=true.
# --------------------------------------------------------------------------- #

_CONTENT_READ_TOOLS = {
    "server_info", "verify_connection", "list_courses", "search_courses",
    "get_my_courses", "get_course_content", "list_quizzes",
    "search_course_materials", "read_activity", "read_lesson",
}
_CONTENT_WRITE_TOOLS = {
    "create_page", "update_page", "create_book", "create_label", "create_url",
    "create_quiz", "add_quiz_question", "add_truefalse_question",
    "add_shortanswer_question", "add_essay_question", "add_numerical_question",
    "add_matching_question", "create_section", "delete_section",
    "update_activity", "move_activity", "delete_activity",
}
_TOOLSETS: dict[str, set[str] | None] = {
    "": None,  # None = no filtering
    "all": None,
    "content": _CONTENT_READ_TOOLS | _CONTENT_WRITE_TOOLS,
}

TOOLSET = os.environ.get("MOODLE_TOOLSET", "").strip().lower()
if TOOLSET not in _TOOLSETS:
    # Fail loudly: silently falling back to every tool would expose more than
    # the operator asked for.
    raise SystemExit(
        f"Unknown MOODLE_TOOLSET {TOOLSET!r}; expected 'content' or unset."
    )
_ENABLED_TOOLS = _TOOLSETS[TOOLSET]


def tool():
    """Like @mcp.tool(), but skips tools outside the active MOODLE_TOOLSET.

    The function is returned unchanged either way, so tools can still call
    each other directly (e.g. daily_briefing -> get_grades).
    """
    def decorator(fn):
        if _ENABLED_TOOLS is None or fn.__name__ in _ENABLED_TOOLS:
            mcp.tool()(fn)
        return fn
    return decorator


class MoodleError(Exception):
    """Raised when Moodle returns an error, even on HTTP 200.

    `errorcode` is Moodle's errorcode when the error came from Moodle.
    """

    def __init__(self, message: str, errorcode: str | None = None) -> None:
        super().__init__(message)
        self.errorcode = errorcode


# Moodle's errorcodes when a function is not available to the token: it does
# not exist on the site (invalidrecord) or is not in the token's service
# (accessexception).
_MISSING_FUNCTION_CODES = {"invalidrecord", "accessexception"}


def _missing_function_message(what: str, wsfunction: str) -> str:
    if wsfunction.startswith(PLUGIN_PREFIX):
        return (
            f"{what} needs {wsfunction}, which this site's plugin version does "
            "not provide (or the token's web service does not include it)."
        )
    return f"{what} needs {wsfunction}, which the token's web service does not include."


async def _available_functions() -> set[str]:
    """Names of the web service functions the token may call."""
    site = await _call("core_webservice_get_site_info")
    return {f.get("name") for f in site.get("functions", [])}


# --------------------------------------------------------------------------- #
# Shared request helper.
# --------------------------------------------------------------------------- #

async def _call(wsfunction: str, params: dict[str, Any] | None = None) -> Any:
    """Call a Moodle web service function and return parsed JSON.

    Moodle returns errors as a JSON object containing "exception"/"errorcode"
    even with an HTTP 200 status, so we detect and raise those as clean errors.
    The token is passed as a POST field and is never logged.
    """
    if not MOODLE_URL or not MOODLE_TOKEN:
        raise MoodleError(
            "MOODLE_URL and MOODLE_TOKEN must be set in the environment."
        )

    data: dict[str, Any] = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
    }
    if params:
        data.update(_flatten(params))

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(REST_ENDPOINT, data=data)
        resp.raise_for_status()

        # Some functions (e.g. deletes) legitimately return null.
        if not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MoodleError(f"Non-JSON response from Moodle: {resp.text[:300]}") from exc

    if isinstance(payload, dict) and "exception" in payload:
        code = payload.get("errorcode", "unknown")
        message = payload.get("message", "Unknown Moodle error")
        debug = payload.get("debuginfo")
        detail = f" ({debug})" if debug else ""
        error = MoodleError(f"Moodle error [{code}]: {message}{detail}", errorcode=code)
        if wsfunction.startswith(PLUGIN_PREFIX) and code in _MISSING_FUNCTION_CODES:
            # Those codes have other causes too, so confirm the function really
            # is missing before replacing Moodle's message with ours. Plugin
            # tools are named after their function, minus the prefix.
            try:
                missing = wsfunction not in await _available_functions()
            except (MoodleError, httpx.HTTPError):
                missing = False
            if missing:
                tool_name = wsfunction[len(PLUGIN_PREFIX):]
                raise MoodleError(
                    _missing_function_message(tool_name, wsfunction), errorcode=code
                ) from error
        raise error

    return payload


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested dict/list params into Moodle's PHP-style form encoding.

    Moodle expects e.g. courses[0][fullname]=... rather than JSON bodies.
    """
    flat: dict[str, str] = {}
    for key, value in params.items():
        composite = f"{prefix}[{key}]" if prefix else str(key)
        _flatten_value(composite, value, flat)
    return flat


def _flatten_value(key: str, value: Any, flat: dict[str, str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten_value(f"{key}[{k}]", v, flat)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _flatten_value(f"{key}[{i}]", item, flat)
    elif isinstance(value, bool):
        flat[key] = "1" if value else "0"
    elif value is not None:
        flat[key] = str(value)


# Form encoding cannot express an empty list: _flatten sends nothing for [],
# and Moodle then reports a confusing "missing required parameter" error. So
# list parameters are checked here, before anything is sent.

def _list_problems(name: str, value: Any, minimum: int) -> list[str]:
    """Describe why `value` is not a list of at least `minimum` items, if it isn't."""
    if not isinstance(value, list):
        return [f"{name} must be a list of at least {minimum}"]
    if len(value) < minimum:
        return [f"{name} needs at least {minimum} item{'s' if minimum > 1 else ''}, "
                f"got {len(value)}"]
    return []


def _require_list(name: str, value: Any, minimum: int = 1) -> None:
    """Raise a clear MoodleError, before calling Moodle, if a list is too short."""
    problems = _list_problems(name, value, minimum)
    if problems:
        raise MoodleError(f"{problems[0]}; nothing was sent to Moodle.")


# The list each question type needs, and its minimum length.
_QUESTION_LISTS = {
    "multichoice": ("answers", 2),
    "shortanswer": ("answers", 1),
    "numerical": ("answers", 1),
    "matching": ("pairs", 3),  # Moodle: at least 2 questions and 3 answers
}


# --------------------------------------------------------------------------- #
# build_course outline validation (pure — no network, unit-testable).
# --------------------------------------------------------------------------- #

_ACTIVITY_TYPES = {
    "page", "label", "url", "forum", "assignment", "choice", "quiz",
}
_QUESTION_TYPES = {
    "multichoice", "truefalse", "shortanswer", "essay", "numerical", "matching",
}


# The plugin function build_course calls for each activity / question type.
_ACTIVITY_FUNCTIONS = {
    "page": PLUGIN_PREFIX + "create_page",
    "label": PLUGIN_PREFIX + "create_label",
    "url": PLUGIN_PREFIX + "create_url",
    "forum": PLUGIN_PREFIX + "create_forum",
    "assignment": PLUGIN_PREFIX + "create_assignment",
    "choice": PLUGIN_PREFIX + "create_choice",
    "quiz": PLUGIN_PREFIX + "create_quiz",
}
_QUESTION_FUNCTIONS = {
    "multichoice": PLUGIN_PREFIX + "add_quiz_question",
    "truefalse": PLUGIN_PREFIX + "add_truefalse_question",
    "shortanswer": PLUGIN_PREFIX + "add_shortanswer_question",
    "essay": PLUGIN_PREFIX + "add_essay_question",
    "numerical": PLUGIN_PREFIX + "add_numerical_question",
    "matching": PLUGIN_PREFIX + "add_matching_question",
}


def _outline_functions(outline: dict, rollback: bool = False) -> list[tuple[str, str]]:
    """List (what, wsfunction) for every function a valid outline will call.

    Pure, so build_course can check them all against the site's function list
    before writing anything. Each function appears once, under the first
    thing that needs it.
    """
    needed: dict[str, str] = {}
    creates_course = not outline.get("courseid")
    if creates_course:
        needed.setdefault("core_course_create_courses", "creating the course")
        if rollback:
            needed.setdefault("core_course_delete_courses", "rollback")
    sections = outline.get("sections", [])
    if sections:
        needed.setdefault(PLUGIN_PREFIX + "create_section", "sections")
    for sec in sections:
        for act in sec.get("activities", []):
            atype = act.get("type")
            needed.setdefault(_ACTIVITY_FUNCTIONS[atype], atype)
            if atype == "quiz":
                for q in act.get("questions", []) or []:
                    qtype = q.get("qtype", "multichoice")
                    needed.setdefault(_QUESTION_FUNCTIONS[qtype], f"{qtype} questions")
    return [(what, fn) for fn, what in needed.items()]


def _validate_outline(outline: Any) -> list[str]:
    """Return a list of human-readable problems with a build_course outline.

    Empty list means the outline is well-formed. Runs before any writes so a
    malformed outline fails fast with a clear message instead of half-building.
    """
    errors: list[str] = []
    if not isinstance(outline, dict):
        return ["outline must be a JSON object"]

    if not outline.get("courseid"):
        for field in ("fullname", "shortname"):
            val = outline.get(field)
            if not isinstance(val, str) or not val.strip():
                errors.append(f"outline.{field} is required (non-empty) when courseid is not given")

    sections = outline.get("sections", [])
    if not isinstance(sections, list):
        return errors + ["outline.sections must be a list"]

    for si, sec in enumerate(sections):
        where = f"sections[{si}]"
        if not isinstance(sec, dict):
            errors.append(f"{where} must be an object")
            continue
        acts = sec.get("activities", [])
        if not isinstance(acts, list):
            errors.append(f"{where}.activities must be a list")
            continue
        for ai, act in enumerate(acts):
            aw = f"{where}.activities[{ai}]"
            if not isinstance(act, dict):
                errors.append(f"{aw} must be an object")
                continue
            atype = act.get("type")
            if atype not in _ACTIVITY_TYPES:
                errors.append(
                    f"{aw}.type {atype!r} is not one of {sorted(_ACTIVITY_TYPES)}")
                continue
            if atype == "choice":
                errors.extend(_list_problems(f"{aw}.options", act.get("options"), 2))
            if atype == "quiz":
                for qi, q in enumerate(act.get("questions", []) or []):
                    qw = f"{aw}.questions[{qi}]"
                    qtype = q.get("qtype", "multichoice") if isinstance(q, dict) else None
                    if not isinstance(q, dict):
                        errors.append(f"{qw} must be an object")
                    elif qtype not in _QUESTION_TYPES:
                        errors.append(
                            f"{qw}.qtype {q.get('qtype')!r} is not one of "
                            f"{sorted(_QUESTION_TYPES)}")
                    elif qtype in _QUESTION_LISTS:
                        key, minimum = _QUESTION_LISTS[qtype]
                        errors.extend(_list_problems(f"{qw}.{key}", q.get(key), minimum))
    return errors


# --------------------------------------------------------------------------- #
# READ TOOLS (always registered).
# --------------------------------------------------------------------------- #

def _package_version() -> str:
    """This package's version, from its installed metadata (pyproject.toml).

    pyproject.toml is the only place the version is written down. Running
    from a checkout that was never installed gives "0+unknown".
    """
    try:
        return _metadata.version("moodle-mcp-connector")
    except _metadata.PackageNotFoundError:
        return "0+unknown"


SERVER_VERSION = _package_version()

# Oldest local_mcpconnector plugin version (its version.php $plugin->version)
# whose functions and return values this server is written against.
MIN_PLUGIN_VERSION = 2026102123


async def _plugin_status(funcs: set) -> dict:
    """Report the plugin's version and whether it is new enough.

    Never raises: any failure is reported as plugin_compatible False with the
    error, so server_info stays usable as a diagnostic.
    """
    status: dict[str, Any] = {
        "plugin_installed": any(f and f.startswith(PLUGIN_PREFIX) for f in funcs),
        "plugin_version": None,
        "plugin_release": None,
        "plugin_compatible": False,
        "plugin_min_version": MIN_PLUGIN_VERSION,
    }
    if not status["plugin_installed"]:
        return status
    try:
        about = await _call(PLUGIN_PREFIX + "get_info")
        version = int(about["version"])
        status.update({
            "plugin_version": version,
            "plugin_release": about.get("release"),
            "plugin_compatible": version >= MIN_PLUGIN_VERSION,
        })
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        status["plugin_error"] = f"{type(exc).__name__}: {exc}"
    return status


@tool()
async def server_info() -> dict:
    """Health check: version, write-mode, config presence, plugin availability.

    Read-only and cheap. Confirms the MCP server is wired up correctly before
    you rely on it: reports this server's version, whether write tools are
    enabled, whether URL/token are configured, and (via a live site-info call)
    whether the local_mcpconnector plugin's functions are present on the Moodle
    site. When they are, it also reports the plugin's version and release
    (from local_mcpconnector_get_info) and plugin_compatible: whether that
    version is at least plugin_min_version. If the version cannot be read,
    plugin_compatible is false and plugin_error says why. If the site cannot
    be reached or rejects the token, connected is false and error says why.
    Never returns the token itself.
    """
    info: dict[str, Any] = {
        "server_version": SERVER_VERSION,
        "write_enabled": ALLOW_WRITE,
        "toolset": TOOLSET or "all",
        "moodle_url_set": bool(MOODLE_URL),
        "moodle_token_set": bool(MOODLE_TOKEN),
    }
    try:
        site = await _call("core_webservice_get_site_info")
        funcs = {f.get("name") for f in site.get("functions", [])}
        info.update({
            "connected": True,
            "sitename": site.get("sitename"),
            "moodle_release": site.get("release"),
            "function_count": len(funcs),
        })
    except MoodleError as exc:
        info.update({"connected": False, "error": str(exc)})
        return info
    except httpx.HTTPError as exc:
        # Unreachable host, timeout, TLS failure or HTTP error status. The
        # token travels in the POST body, so the message cannot contain it.
        # Some httpx errors have no message, hence the type name.
        info.update({"connected": False, "error": f"{type(exc).__name__}: {exc}".rstrip(": ")})
        return info
    info.update(await _plugin_status(funcs))
    return info


@tool()
async def verify_connection() -> dict:
    """Verify the token/URL work and return Moodle site info.

    Read-only. Call this first to confirm connectivity and see which site,
    version, and user the token is bound to.
    """
    return await _call("core_webservice_get_site_info")


@tool()
async def list_courses() -> list:
    """List all courses on the site the token can see.

    Read-only. Returns id, fullname, shortname, categoryid, summary, etc.
    """
    return await _call("core_course_get_courses")


@tool()
async def search_courses(field: str, value: str) -> dict:
    """Search courses by a single field.

    Read-only. `field` is one of: id, ids, shortname, idnumber, category.
    Example: field="shortname", value="CS101".
    """
    return await _call("core_course_get_courses_by_field", {"field": field, "value": value})


@tool()
async def get_course_content(courseid: int) -> list:
    """Get the full section/activity structure of one course.

    Read-only. Returns sections, each with the modules (activities/resources)
    inside it — useful for seeing what already exists before adding content.
    """
    return await _call("core_course_get_contents", {"courseid": courseid})


@tool()
async def get_enrolled_users(courseid: int) -> list:
    """List users enrolled in a course.

    Read-only. Returns each user's id, fullname, email, and roles.
    """
    return await _call("core_enrol_get_enrolled_users", {"courseid": courseid})


@tool()
async def list_quizzes(courseids: list[int]) -> dict:
    """List quizzes in one or more courses.

    Read-only. Pass a list of course IDs.
    """
    _require_list("courseids", courseids)
    return await _call("mod_quiz_get_quizzes_by_courses", {"courseids": courseids})


@tool()
async def get_user_grades(courseid: int, userid: int = 0) -> dict:
    """Get grade items for a user in a course.

    Read-only. If userid is 0, returns grades for the token's own user where
    permitted. Otherwise returns the specified user's grade items.
    """
    params: dict[str, Any] = {"courseid": courseid}
    if userid:
        params["userid"] = userid
    return await _call("gradereport_user_get_grade_items", params)


# --------------------------------------------------------------------------- #
# STUDENT / ANALYSIS READ TOOLS (always registered).
#
# These build on the raw web service functions above to give an AI assistant a
# student's-eye view: courses, assignments, deadlines, grades, progress, and
# combined dashboards. The "analysis" tools gather and structure the relevant
# Moodle data; the calling AI does the natural-language reasoning over it.
#
# They rely on extra web service functions being present in the token's service:
#   core_enrol_get_users_courses, mod_assign_get_assignments,
#   mod_assign_get_submission_status, gradereport_overview_get_course_grades,
#   core_calendar_get_action_events_by_timesort, mod_forum_get_forums_by_courses,
#   mod_forum_get_forum_discussions, core_completion_get_activities_completion_status
# --------------------------------------------------------------------------- #

_STOPWORDS = {
    "the", "and", "for", "you", "your", "this", "that", "with", "from", "have",
    "will", "are", "was", "not", "but", "all", "can", "our", "out", "use", "any",
    "how", "who", "why", "what", "when", "which", "into", "than", "then", "them",
    "should", "must", "may", "shall", "each", "also", "using", "used", "one", "two",
    "assignment", "assignments", "task", "please", "student", "students", "submit",
}


def _now() -> int:
    return int(_time.time())


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _week_label(ts: int) -> str:
    iso = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = _htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _keywords(text: str, limit: int = 12) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", (text or "").lower())
    result: list[str] = []
    for w in words:
        if w not in _STOPWORDS and w not in result:
            result.append(w)
    return result[:limit]


_USERID_CACHE: dict[str, int] = {}


async def _get_userid() -> int:
    if "id" not in _USERID_CACHE:
        info = await _call("core_webservice_get_site_info")
        _USERID_CACHE["id"] = int(info.get("userid", 0))
    return _USERID_CACHE["id"]


async def _my_courses_raw() -> list:
    uid = await _get_userid()
    return await _call("core_enrol_get_users_courses", {"userid": uid}) or []


async def _assignments_raw(courseids: list[int] | None = None) -> list:
    if not courseids:
        courseids = [c["id"] for c in await _my_courses_raw()]
    data = await _call("mod_assign_get_assignments", {"courseids": courseids})
    out = []
    for c in data.get("courses", []):
        for a in c.get("assignments", []):
            a["coursename"] = c.get("fullname") or c.get("shortname")
            out.append(a)
    return out


def _is_submitted(status: dict) -> bool:
    last = (status or {}).get("lastattempt") or {}
    sub = last.get("submission") or {}
    return sub.get("status") == "submitted"


def _grading_status(status: dict) -> str | None:
    last = (status or {}).get("lastattempt") or {}
    return last.get("gradingstatus")


async def _enriched_assignments(courseids: list[int] | None = None) -> list:
    """All assignments that have a due date, enriched with submission status."""
    uid = await _get_userid()
    assigns = [a for a in await _assignments_raw(courseids) if a.get("duedate")]

    async def enrich(a: dict) -> dict:
        submitted, grading = False, None
        try:
            st = await _call(
                "mod_assign_get_submission_status",
                {"assignid": a["id"], "userid": uid},
            )
            submitted = _is_submitted(st)
            grading = _grading_status(st)
        except MoodleError:
            pass  # Some assignments do not expose status; treat as unknown.
        due = a.get("duedate") or 0
        return {
            "assignid": a["id"],
            "cmid": a.get("cmid"),
            "name": a.get("name"),
            "courseid": a.get("course"),
            "coursename": a.get("coursename"),
            "duedate": due,
            "due_iso": _iso(due),
            "days_left": round((due - _now()) / 86400, 1) if due else None,
            "submitted": submitted,
            "gradingstatus": grading,
        }

    return list(await asyncio.gather(*[enrich(a) for a in assigns]))


async def _course_materials(courseid: int) -> list:
    contents = await _call("core_course_get_contents", {"courseid": courseid})
    mats = []
    for sec in contents or []:
        for m in sec.get("modules", []):
            mats.append({
                "courseid": courseid,
                "section": sec.get("name"),
                "cmid": m.get("id"),
                "name": m.get("name"),
                "modname": m.get("modname"),
                "url": m.get("url"),
                "description": _strip_html(m.get("description", "")),
            })
    return mats


async def _course_progress(courseid: int, uid: int) -> dict:
    try:
        data = await _call(
            "core_completion_get_activities_completion_status",
            {"courseid": courseid, "userid": uid},
        )
        statuses = data.get("statuses", [])
        total = len(statuses)
        done = sum(1 for s in statuses if s.get("state") == 1)
        return {
            "courseid": courseid,
            "activities_total": total,
            "activities_completed": done,
            "percent": round(100 * done / total, 1) if total else None,
        }
    except MoodleError:
        return {"courseid": courseid, "percent": None, "note": "completion tracking not available"}


async def _find_assignment(assignid: int) -> dict | None:
    for a in await _assignments_raw():
        if a["id"] == assignid:
            return a
    return None


# --- Course & content ------------------------------------------------------ #

@tool()
async def get_my_courses() -> list:
    """Get all courses the current user (the token's user) is enrolled in.

    Read-only. Returns id, fullname, shortname, and progress where available.
    """
    courses = await _my_courses_raw()
    return [
        {
            "id": c.get("id"),
            "fullname": c.get("fullname"),
            "shortname": c.get("shortname"),
            "progress": c.get("progress"),
            "startdate": _iso(c.get("startdate")),
            "enddate": _iso(c.get("enddate")),
        }
        for c in courses
    ]


@tool()
async def search_course_materials(query: str, courseid: int = 0) -> list:
    """Search across course materials by a text query.

    Read-only. Matches the query (case-insensitive) against activity names,
    descriptions, and section names. Searches one course if courseid is given,
    otherwise all of the user's courses. Returns matching materials.
    """
    q = query.lower().strip()
    course_ids = [courseid] if courseid else [c["id"] for c in await _my_courses_raw()]
    lists = await asyncio.gather(*[_course_materials(cid) for cid in course_ids])
    hits = []
    for mats in lists:
        for m in mats:
            haystack = f"{m['name']} {m['description']} {m['section'] or ''}".lower()
            if q in haystack:
                hits.append(m)
    return hits


@tool()
async def get_course_announcements(courseid: int = 0) -> list:
    """Get announcements from course news forums.

    Read-only. Reads each course's Announcements (news) forum. Filtered to one
    course if courseid is given, otherwise all of the user's courses. Returns
    the most recent discussions (subject, message, author, time).
    """
    course_ids = [courseid] if courseid else [c["id"] for c in await _my_courses_raw()]
    forums = await _call("mod_forum_get_forums_by_courses", {"courseids": course_ids})
    news = [f for f in (forums or []) if f.get("type") == "news"]
    out = []
    for f in news:
        try:
            disc = await _call("mod_forum_get_forum_discussions", {"forumid": f["id"]})
        except MoodleError:
            continue
        for d in disc.get("discussions", []):
            out.append({
                "courseid": f.get("course"),
                "subject": d.get("subject"),
                "message": _strip_html(d.get("message", "")),
                "author": d.get("userfullname"),
                "modified": _iso(d.get("timemodified")),
                "timemodified": d.get("timemodified"),
            })
    out.sort(key=lambda x: x.get("timemodified") or 0, reverse=True)
    return out


@tool()
async def get_recent_activity(since: int = 0) -> list:
    """Get recent activity across courses since a given time.

    Read-only. `since` is a unix timestamp; defaults to the last 7 days.
    Aggregates recent announcements and recently updated assignments.
    """
    if not since:
        since = _now() - 7 * 86400
    events: list[dict] = []
    for a in await _assignments_raw():
        tm = a.get("timemodified") or 0
        if tm >= since:
            events.append({
                "type": "assignment",
                "title": a.get("name"),
                "courseid": a.get("course"),
                "coursename": a.get("coursename"),
                "time": _iso(tm),
                "timestamp": tm,
            })
    for ann in await get_course_announcements():
        if (ann.get("timemodified") or 0) >= since:
            events.append({
                "type": "announcement",
                "title": ann.get("subject"),
                "courseid": ann.get("courseid"),
                "time": ann.get("modified"),
                "timestamp": ann.get("timemodified"),
            })
    events.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
    return events


# --- Assignments & deadlines ----------------------------------------------- #

@tool()
async def get_assignments(courseids: list[int] | None = None) -> list:
    """Get assignments for courses.

    Read-only. Pass a list of course IDs, or omit to use all enrolled courses.
    Returns id, cmid, name, course, due date, and open/cutoff dates.
    """
    return [
        {
            "assignid": a.get("id"),
            "cmid": a.get("cmid"),
            "name": a.get("name"),
            "courseid": a.get("course"),
            "coursename": a.get("coursename"),
            "duedate": a.get("duedate"),
            "due_iso": _iso(a.get("duedate")),
            "allowsubmissionsfromdate": _iso(a.get("allowsubmissionsfromdate")),
            "cutoffdate": _iso(a.get("cutoffdate")),
        }
        for a in await _assignments_raw(courseids)
    ]


@tool()
async def get_assignment_status(assignid: int) -> dict:
    """Get submission and grading status for a specific assignment.

    Read-only. Returns whether the current user has submitted, the grading
    status, and the grade if one has been released.
    """
    uid = await _get_userid()
    st = await _call("mod_assign_get_submission_status", {"assignid": assignid, "userid": uid})
    last = st.get("lastattempt") or {}
    feedback = st.get("feedback") or {}
    return {
        "assignid": assignid,
        "submitted": _is_submitted(st),
        "gradingstatus": _grading_status(st),
        "grade": (feedback.get("grade") or {}).get("grade"),
        "cansubmit": last.get("cansubmit"),
        "graded": bool(feedback.get("grade")),
    }


@tool()
async def get_upcoming_deadlines(within_days: int = 0) -> list:
    """Get upcoming assignment deadlines across all courses, soonest first.

    Read-only. If within_days > 0, only deadlines within that many days are
    returned. Each item includes days_left and whether it is already submitted.
    """
    now = _now()
    tasks = [t for t in await _enriched_assignments() if t["duedate"] >= now]
    if within_days:
        cutoff = now + within_days * 86400
        tasks = [t for t in tasks if t["duedate"] <= cutoff]
    tasks.sort(key=lambda t: t["duedate"])
    return tasks


@tool()
async def get_overdue_assignments() -> list:
    """Get unsubmitted assignments past their due date, most overdue first.

    Read-only. Only assignments with a due date in the past that the user has
    not submitted are returned.
    """
    now = _now()
    tasks = [
        t for t in await _enriched_assignments()
        if 0 < t["duedate"] < now and not t["submitted"]
    ]
    tasks.sort(key=lambda t: t["duedate"])
    return tasks


@tool()
async def get_actionable_tasks() -> list:
    """Get a prioritized list of tasks needing action, most urgent first.

    Read-only. Includes any unsubmitted assignment with a due date. Each task is
    tagged with an urgency level (overdue / due_soon / upcoming).
    """
    tasks = [t for t in await _enriched_assignments() if t["duedate"] and not t["submitted"]]
    for t in tasks:
        days = t["days_left"]
        if days is not None and days < 0:
            t["urgency"] = "overdue"
        elif days is not None and days <= 3:
            t["urgency"] = "due_soon"
        else:
            t["urgency"] = "upcoming"
    tasks.sort(key=lambda t: t["duedate"])
    return tasks


@tool()
async def analyze_assignment(assignid: int) -> dict:
    """Analyze an assignment: status, requirements, materials, progress, deadline.

    Read-only. Gathers the assignment's description, submission/grading status,
    deadline math, and the course materials most relevant to it, so the AI can
    reason about what to do next. Does not itself write anything.
    """
    a = await _find_assignment(assignid)
    if not a:
        raise MoodleError(f"Assignment {assignid} not found in the user's courses.")
    uid = await _get_userid()
    submitted, grading = False, None
    try:
        st = await _call("mod_assign_get_submission_status", {"assignid": assignid, "userid": uid})
        submitted, grading = _is_submitted(st), _grading_status(st)
    except MoodleError:
        pass
    due = a.get("duedate") or 0
    return {
        "assignid": assignid,
        "name": a.get("name"),
        "courseid": a.get("course"),
        "coursename": a.get("coursename"),
        "description": _strip_html(a.get("intro", "")),
        "attachments": [f.get("filename") for f in a.get("introattachments", [])],
        "duedate": due,
        "due_iso": _iso(due),
        "days_left": round((due - _now()) / 86400, 1) if due else None,
        "submitted": submitted,
        "gradingstatus": grading,
        "relevant_materials": await find_relevant_materials(assignid),
    }


@tool()
async def extract_assignment_requirements(assignid: int) -> dict:
    """Extract the source text of an assignment for requirement analysis.

    Read-only. Returns the assignment's full description text and attachments so
    the calling AI can identify requirements, deliverables, constraints, and
    evaluation criteria. The `sentences` list is a convenience splitting of the
    description; the AI should interpret them into structured requirements.
    """
    a = await _find_assignment(assignid)
    if not a:
        raise MoodleError(f"Assignment {assignid} not found in the user's courses.")
    text = _strip_html(a.get("intro", ""))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return {
        "assignid": assignid,
        "name": a.get("name"),
        "description": text,
        "sentences": sentences,
        "attachments": [f.get("filename") for f in a.get("introattachments", [])],
    }


@tool()
async def find_relevant_materials(assignid: int) -> list:
    """Find course materials relevant to an assignment, ranked by relevance.

    Read-only. Pulls keywords from the assignment title and description, then
    scores the materials in the same course by keyword overlap. Returns the
    matches sorted by score (most relevant first).
    """
    a = await _find_assignment(assignid)
    if not a:
        raise MoodleError(f"Assignment {assignid} not found in the user's courses.")
    keywords = _keywords(f"{a.get('name', '')} {_strip_html(a.get('intro', ''))}")
    mats = await _course_materials(a.get("course"))
    scored = []
    for m in mats:
        blob = f"{m['name']} {m['description']}".lower()
        score = sum(1 for kw in keywords if kw in blob)
        if score:
            scored.append({**m, "score": score})
    scored.sort(key=lambda m: m["score"], reverse=True)
    return scored


@tool()
async def decompose_task(assignid: int) -> dict:
    """Break an assignment into subtasks with effort, dependencies, timeline.

    Read-only. Returns the assignment context plus the number of days available
    before the deadline, as a scaffold. The calling AI should populate the
    subtasks, effort estimates, dependencies, and critical path from this data.
    """
    ctx = await analyze_assignment(assignid)
    days = ctx.get("days_left")
    return {
        "assignment": ctx,
        "days_available": days,
        "guidance": (
            "Break this assignment into ordered subtasks. For each, estimate "
            "effort (hours), list dependencies, and flag those on the critical "
            "path. Fit the total effort within days_available before the deadline."
        ),
        "subtasks": [],  # To be filled in by the AI from the context above.
    }


@tool()
async def create_implementation_plan(assignid: int) -> dict:
    """Build a step-by-step plan with timeline, milestones, and risks.

    Read-only. Returns the assignment context and four evenly spaced milestone
    dates between now and the deadline, as a scaffold for the AI to turn into a
    concrete plan (steps, resources, milestones, risks).
    """
    ctx = await analyze_assignment(assignid)
    now = _now()
    due = ctx.get("duedate") or 0
    milestones = []
    if due > now:
        span = due - now
        for i in range(1, 5):
            milestones.append({"milestone": i, "target_date": _iso(now + span * i // 4)})
    return {
        "assignment": ctx,
        "suggested_milestones": milestones,
        "guidance": (
            "Produce a step-by-step plan: for each step give a title, the "
            "resources/materials needed (use relevant_materials), the milestone "
            "it maps to, and any risks. Keep it within the suggested_milestones."
        ),
        "steps": [],  # To be filled in by the AI.
    }


# --- Grades & progress ----------------------------------------------------- #

@tool()
async def get_grades(courseid: int = 0) -> Any:
    """Get a grade overview for all courses, or detailed grades for one course.

    Read-only. With no courseid, returns the cross-course grade overview. With a
    courseid, returns the detailed grade items for that course.
    """
    if courseid:
        uid = await _get_userid()
        return await _call("gradereport_user_get_grade_items", {"courseid": courseid, "userid": uid})
    return await _call("gradereport_overview_get_course_grades")


@tool()
async def get_course_progress(courseid: int = 0) -> Any:
    """Get progress/completion for one course or all courses.

    Read-only. Requires completion tracking to be enabled on the course(s).
    Returns activities completed vs total and a percentage.
    """
    uid = await _get_userid()
    if courseid:
        return await _course_progress(courseid, uid)
    course_ids = [c["id"] for c in await _my_courses_raw()]
    return list(await asyncio.gather(*[_course_progress(cid, uid) for cid in course_ids]))


@tool()
async def get_course_health(courseid: int) -> dict:
    """Health check for a course: progress, grade, unsubmitted and overdue counts.

    Read-only. Combines completion, the overview grade, and assignment status
    into a single snapshot for one course.
    """
    now = _now()
    uid = await _get_userid()
    prog = await _course_progress(courseid, uid)
    enriched = await _enriched_assignments([courseid])
    unsubmitted = [t for t in enriched if not t["submitted"]]
    overdue = [t for t in enriched if 0 < t["duedate"] < now and not t["submitted"]]
    grade = None
    try:
        overview = await _call("gradereport_overview_get_course_grades")
        row = next((g for g in overview.get("grades", []) if g.get("courseid") == courseid), None)
        grade = row.get("grade") if row else None
    except MoodleError:
        pass
    return {
        "courseid": courseid,
        "percent_complete": prog.get("percent"),
        "grade": grade,
        "unsubmitted_count": len(unsubmitted),
        "overdue_count": len(overdue),
    }


@tool()
async def get_study_load() -> list:
    """Analyze assignment distribution by week to spot heavy weeks.

    Read-only. Buckets upcoming assignments by ISO week and counts them, so the
    AI can flag weeks with an unusually heavy workload.
    """
    buckets: dict[str, dict] = {}
    for t in await _enriched_assignments():
        if not t["duedate"]:
            continue
        label = _week_label(t["duedate"])
        b = buckets.setdefault(label, {"week": label, "count": 0, "assignments": []})
        b["count"] += 1
        b["assignments"].append(t["name"])
    return sorted(buckets.values(), key=lambda b: b["week"])


# --- Aggregated overviews -------------------------------------------------- #

@tool()
async def get_upcoming_events(limit: int = 20) -> list:
    """Get upcoming calendar events from Moodle, soonest first.

    Read-only. Uses Moodle's action-events calendar view from now onward.
    """
    data = await _call(
        "core_calendar_get_action_events_by_timesort",
        {"timesortfrom": _now(), "limitnum": limit},
    )
    events = data.get("events", []) if isinstance(data, dict) else []
    return [
        {
            "name": e.get("name"),
            "timestart": _iso(e.get("timestart")),
            "timesort": e.get("timesort"),
            "courseid": (e.get("course") or {}).get("id"),
            "url": (e.get("action") or {}).get("url") or e.get("url"),
        }
        for e in events
    ]


@tool()
async def semester_dashboard() -> dict:
    """Combined overview of courses, upcoming deadlines, and grades.

    Read-only. A one-call snapshot for a "how is my semester going?" question.
    """
    courses, deadlines, grades = await asyncio.gather(
        get_my_courses(),
        get_upcoming_deadlines(),
        get_grades(),
    )
    return {
        "courses": courses,
        "upcoming_deadlines": deadlines[:10],
        "grades": grades,
    }


@tool()
async def daily_briefing() -> dict:
    """Daily summary: overdue count, today's deadlines, recent grades, events, tasks.

    Read-only. A compact "what do I need to know today?" digest.
    """
    now = _now()
    overdue, tasks, events, grades = await asyncio.gather(
        get_overdue_assignments(),
        get_actionable_tasks(),
        get_upcoming_events(5),
        get_grades(),
    )
    todays = [t for t in tasks if t["days_left"] is not None and 0 <= t["days_left"] < 1]
    return {
        "date": _iso(now),
        "overdue_count": len(overdue),
        "todays_deadlines": todays,
        "recent_grades": grades,
        "upcoming_events": events,
        "top_tasks": tasks[:5],
    }


@tool()
async def weekly_review() -> dict:
    """Weekly summary: submitted/graded counts, deadlines, overdue count, progress.

    Read-only. A "how did this week go and what's next?" digest.
    """
    enriched = await _enriched_assignments()
    now = _now()
    submitted = [t for t in enriched if t["submitted"]]
    graded = [t for t in enriched if t["gradingstatus"] == "graded"]
    overdue = [t for t in enriched if 0 < t["duedate"] < now and not t["submitted"]]
    this_week = [t for t in enriched if t["days_left"] is not None and 0 <= t["days_left"] <= 7]
    progress = await get_course_progress()
    percents = [p.get("percent") for p in progress if p.get("percent") is not None]
    avg = round(sum(percents) / len(percents), 1) if percents else None
    return {
        "submitted_count": len(submitted),
        "graded_count": len(graded),
        "deadlines_this_week": sorted(this_week, key=lambda t: t["duedate"]),
        "overdue_count": len(overdue),
        "average_progress_percent": avg,
    }


@tool()
async def ask_moodle(question: str) -> dict:
    """Ask a natural-language question and have it routed to the right data.

    Read-only. Picks the most relevant data source based on the question's
    wording and returns that data under `data`, tagged with `routed_to`. The
    calling AI then answers the question from the returned data.
    """
    q = question.lower()
    if any(w in q for w in ("overdue", "late", "missed", "missing")):
        source, data = "get_overdue_assignments", await get_overdue_assignments()
    elif any(w in q for w in ("deadline", "due", "upcoming assignment", "soon")):
        source, data = "get_upcoming_deadlines", await get_upcoming_deadlines()
    elif any(w in q for w in ("grade", "mark", "score", "result")):
        source, data = "get_grades", await get_grades()
    elif any(w in q for w in ("progress", "complete", "completion")):
        source, data = "get_course_progress", await get_course_progress()
    elif any(w in q for w in ("announce", "news")):
        source, data = "get_course_announcements", await get_course_announcements()
    elif any(w in q for w in ("event", "calendar")):
        source, data = "get_upcoming_events", await get_upcoming_events()
    elif any(w in q for w in ("task", "todo", "to-do", "action", "do next")):
        source, data = "get_actionable_tasks", await get_actionable_tasks()
    elif any(w in q for w in ("course", "class", "enrol", "enroll")):
        source, data = "get_my_courses", await get_my_courses()
    else:
        source, data = "daily_briefing", await daily_briefing()
    return {"question": question, "routed_to": source, "data": data}


# --- Quiz results ----------------------------------------------------------- #

@tool()
async def get_quiz_results(quizid: int, userid: int = 0) -> dict:
    """See how a user did on a quiz: all attempts plus their best grade.

    Read-only. `quizid` is the quiz instance id (from list_quizzes — NOT the
    cmid). `userid` 0 means the current user; teachers can pass any student's
    id. Each attempt includes its id (usable with get_quiz_attempt_review),
    state, timing and summed grade.
    """
    params: dict[str, Any] = {"quizid": quizid, "status": "all"}
    if userid:
        params["userid"] = userid
    attempts = await _call("mod_quiz_get_user_attempts", params)
    best = await _call(
        "mod_quiz_get_user_best_grade",
        {"quizid": quizid, **({"userid": userid} if userid else {})},
    )
    return {
        "quizid": quizid,
        "userid": userid or "current",
        "bestgrade": best.get("grade") if best.get("hasgrade") else None,
        "gradetopass": best.get("gradetopass"),
        "attempts": [
            {
                "attemptid": a.get("id"),
                "attempt": a.get("attempt"),
                "state": a.get("state"),
                "started": _iso(a.get("timestart")),
                "finished": _iso(a.get("timefinish")),
                "sumgrades": a.get("sumgrades"),
            }
            for a in attempts.get("attempts", [])
        ],
    }


@tool()
async def get_quiz_attempt_review(attemptid: int) -> dict:
    """Review a finished quiz attempt question by question.

    Read-only. `attemptid` comes from get_quiz_results. Returns the grade and,
    for each question, its text, the response summary and the mark obtained.
    Requires the attempt to be finished and the caller to be allowed to review
    it (students: their own attempts; teachers: any).
    """
    review = await _call("mod_quiz_get_attempt_review", {"attemptid": attemptid})
    return {
        "attemptid": attemptid,
        "grade": review.get("grade"),
        "state": (review.get("attempt") or {}).get("state"),
        "questions": [
            {
                "slot": q.get("slot"),
                "type": q.get("type"),
                "question": _strip_html(q.get("html"))[:500],
                "status": q.get("status"),
                "mark": q.get("mark"),
                "maxmark": q.get("maxmark"),
            }
            for q in review.get("questions", [])
        ],
    }


@tool()
async def get_quiz_report(quizid: int, courseid: int, include_questions: bool = True) -> dict:
    """Whole-class report for a quiz: distribution, averages, who hasn't sat it.

    Read-only. `quizid` is the quiz instance id (from list_quizzes); `courseid`
    is the course it belongs to (needed to know who's enrolled). Returns:
    attempted vs not-attempted counts, the list of students who haven't
    attempted, a 0-20/.../80-100 grade distribution, and average/min/max/pass
    rate. With `include_questions=True` it also returns a per-question facility
    index (average fraction of the marks earned — low = hard question) computed
    from each student's most recent finished attempt.

    Teacher-oriented: the token's user needs the capability to view other
    users' attempts (grade:viewreports) or results are limited to their own.
    """
    users = await _call("core_enrol_get_enrolled_users", {"courseid": courseid})
    best = await _call("mod_quiz_get_user_best_grade", {"quizid": quizid})
    gradetopass = best.get("gradetopass")

    async def user_attempts(uid: int) -> dict:
        data = await _call(
            "mod_quiz_get_user_attempts",
            {"quizid": quizid, "userid": uid, "status": "all"},
        )
        return {"userid": uid, "attempts": data.get("attempts", [])}

    results = await asyncio.gather(*(user_attempts(u["id"]) for u in users))
    by_name = {u["id"]: u.get("fullname", str(u["id"])) for u in users}

    grades: list[float] = []
    not_attempted: list[str] = []
    finished_attempt_ids: list[tuple[int, int]] = []  # (userid, attemptid)
    for r in results:
        finished = [a for a in r["attempts"] if a.get("state") == "finished"]
        if not finished:
            not_attempted.append(by_name[r["userid"]])
            continue
        latest = max(finished, key=lambda a: a.get("timefinish") or 0)
        if latest.get("sumgrades") is not None:
            grades.append(float(latest["sumgrades"]))
        if latest.get("id"):
            finished_attempt_ids.append((r["userid"], latest["id"]))

    buckets = {"0-20": 0, "20-40": 0, "40-60": 0, "60-80": 0, "80-100": 0}
    maxgrade = max(grades) if grades else 0
    for g in grades:
        pct = (g / maxgrade * 100) if maxgrade else 0
        key = ("80-100" if pct > 80 else "60-80" if pct > 60 else
               "40-60" if pct > 40 else "20-40" if pct > 20 else "0-20")
        buckets[key] += 1

    passed = sum(1 for g in grades if gradetopass and g >= float(gradetopass))
    report: dict[str, Any] = {
        "quizid": quizid,
        "courseid": courseid,
        "enrolled": len(users),
        "attempted": len(grades),
        "not_attempted_count": len(not_attempted),
        "not_attempted": not_attempted,
        "average": round(sum(grades) / len(grades), 2) if grades else None,
        "min": min(grades) if grades else None,
        "max": max(grades) if grades else None,
        "gradetopass": gradetopass,
        "pass_rate": (round(passed / len(grades) * 100, 1) if grades else None),
        "distribution": buckets,
    }

    if include_questions and finished_attempt_ids:
        async def review(pair: tuple[int, int]) -> dict:
            return await _call("mod_quiz_get_attempt_review", {"attemptid": pair[1]})

        reviews = await asyncio.gather(*(review(p) for p in finished_attempt_ids),
                                       return_exceptions=True)
        agg: dict[int, dict] = {}
        for rv in reviews:
            if isinstance(rv, Exception):
                continue
            for q in rv.get("questions", []):
                slot = q.get("slot")
                mark, maxmark = q.get("mark"), q.get("maxmark")
                if slot is None or maxmark in (None, 0) or mark is None:
                    continue
                a = agg.setdefault(slot, {"slot": slot, "type": q.get("type"),
                                          "question": _strip_html(q.get("html"))[:160],
                                          "fractions": []})
                a["fractions"].append(float(mark) / float(maxmark))
        report["questions"] = [
            {
                "slot": a["slot"],
                "type": a["type"],
                "question": a["question"],
                "facility": round(sum(a["fractions"]) / len(a["fractions"]), 2),
                "responses": len(a["fractions"]),
            }
            for a in sorted(agg.values(), key=lambda x: x["slot"])
        ]

    return report


# --- Completion tracking ---------------------------------------------------- #

@tool()
async def get_activity_completion(courseid: int, userid: int = 0) -> list:
    """See which activities a user has completed in a course.

    Read-only. `userid` 0 means the current user. Returns one entry per
    activity with its cmid, name, whether completion is manual or automatic,
    and the completion state (0 = incomplete, 1 = complete, 2 = complete with
    pass, 3 = complete with fail).
    """
    uid = userid or await _get_userid()
    data = await _call(
        "core_completion_get_activities_completion_status",
        {"courseid": courseid, "userid": uid},
    )
    return [
        {
            "cmid": s.get("cmid"),
            "activity": s.get("modname"),
            "state": s.get("state"),
            "completed": s.get("state", 0) in (1, 2),
            "tracking": "manual" if s.get("tracking") == 1 else "automatic",
            "timecompleted": _iso(s.get("timecompleted")),
        }
        for s in data.get("statuses", [])
    ]


@tool()
async def get_course_completion_status(courseid: int, userid: int = 0) -> dict:
    """See whether a user has completed a whole course and which criteria remain.

    Read-only. `userid` 0 means the current user. The course must have
    completion criteria configured, otherwise Moodle returns an error.
    """
    uid = userid or await _get_userid()
    data = await _call(
        "core_completion_get_course_completion_status",
        {"courseid": courseid, "userid": uid},
    )
    status = data.get("completionstatus", {})
    return {
        "courseid": courseid,
        "userid": uid,
        "completed": status.get("completed"),
        "criteria": [
            {
                "title": c.get("title"),
                "status": _strip_html(c.get("status")),
                "complete": c.get("complete"),
            }
            for c in status.get("completions", [])
        ],
    }


# --- Notifications ---------------------------------------------------------- #

@tool()
async def get_notifications(limit: int = 20, unread_only: bool = False) -> dict:
    """Read your Moodle notifications (new grades, forum posts, due dates...).

    Read-only. Returns the unread count plus the most recent notifications
    (newest first), each with its subject, a plain-text preview, when it
    arrived and whether it is still unread.
    """
    uid = await _get_userid()
    unread = await _call(
        "message_popup_get_unread_popup_notification_count", {"useridto": uid}
    )
    data = await _call(
        "message_popup_get_popup_notifications",
        {
            "useridto": uid,
            "newestfirst": 1,
            "limit": limit,
            "offset": 0,
        },
    )
    notifications = data.get("notifications", [])
    if unread_only:
        notifications = [n for n in notifications if not n.get("read")]
    return {
        "unreadcount": unread,
        "notifications": [
            {
                "subject": n.get("subject"),
                "preview": _strip_html(n.get("smallmessage") or n.get("fullmessage"))[:300],
                "received": _iso(n.get("timecreated")),
                "unread": not n.get("read"),
                "url": n.get("contexturl") or None,
            }
            for n in notifications
        ],
    }


# --- Competencies & learning plans ------------------------------------------ #

@tool()
async def list_course_competencies(courseid: int) -> list:
    """List the competencies attached to a course.

    Read-only. Returns each competency's id, name, description and id number.
    Empty list if the course uses no competencies.
    """
    data = await _call(
        "core_competency_list_course_competencies", {"id": courseid}
    )
    return [
        {
            "competencyid": (c.get("competency") or {}).get("id"),
            "shortname": (c.get("competency") or {}).get("shortname"),
            "idnumber": (c.get("competency") or {}).get("idnumber"),
            "description": _strip_html((c.get("competency") or {}).get("description")),
        }
        for c in data
    ]


@tool()
async def get_learning_plans(userid: int = 0) -> list:
    """List a user's learning plans (competency-based development plans).

    Read-only. `userid` 0 means the current user. Returns each plan's id,
    name, status and due date. Empty list if the user has no plans.
    """
    uid = userid or await _get_userid()
    data = await _call("core_competency_list_user_plans", {"userid": uid})
    return [
        {
            "planid": p.get("id"),
            "name": p.get("name"),
            "status": p.get("statusname"),
            "duedate": _iso(p.get("duedate")),
        }
        for p in data
    ]


# --- Content reading -------------------------------------------------------- #

_BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "ul", "ol", "tr", "table", "section",
    "article", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "footer", "figure", "figcaption", "dd", "dt",
}


class _TextExtractor(HTMLParser):
    """Collect visible text, turning block-level tags into line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag != "li":
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str | None) -> str:
    """Convert HTML to readable plain text, keeping paragraph breaks.

    Unlike _strip_html (which collapses everything onto one line), this keeps
    the document's block structure so long pages stay readable.
    """
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _is_moodle_url(url: str) -> bool:
    """True if `url` points inside the configured MOODLE_URL site."""
    if not MOODLE_URL or not url.startswith(MOODLE_URL + "/"):
        return False
    # Belt and braces: the parsed host must match too.
    return urlsplit(url).netloc.lower() == urlsplit(MOODLE_URL).netloc.lower()


async def _fetch_file(client: httpx.AsyncClient, fileurl: str) -> httpx.Response:
    """Download a Moodle file (e.g. a Page's index.html) with the token.

    The token is appended as `token=` as Moodle's webservice/pluginfile.php
    expects. It is only ever sent to the configured MOODLE_URL site, and the
    URL (which contains the token) is never put in an error message.
    """
    if not _is_moodle_url(fileurl):
        raise MoodleError("Refusing to send the token to a file URL outside MOODLE_URL.")
    sep = "&" if "?" in fileurl else "?"
    try:
        resp = await client.get(f"{fileurl}{sep}token={MOODLE_TOKEN}")
    except httpx.HTTPError as exc:
        raise MoodleError(f"File download failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise MoodleError(f"File download failed with HTTP {resp.status_code}.")
    if "json" in resp.headers.get("content-type", ""):
        # pluginfile.php reports errors (bad token, files not enabled) as JSON.
        try:
            err = resp.json()
        except ValueError:
            err = {}
        if isinstance(err, dict) and ("error" in err or "exception" in err):
            raise MoodleError(
                f"File download refused: {err.get('error') or err.get('message')}. "
                "Check that the web service allows file downloads."
            )
    return resp


async def _download_text(client: httpx.AsyncClient, fileurl: str) -> str:
    """Download a Moodle file and return its text (see _fetch_file)."""
    return (await _fetch_file(client, fileurl)).text


async def _find_module(course_id: int, cmid: int) -> dict:
    """Return the core_course_get_contents entry for one course module."""
    sections = await _call(
        "core_course_get_contents",
        {"courseid": course_id, "options": [{"name": "cmid", "value": cmid}]},
    )
    for sec in sections or []:
        for m in sec.get("modules", []):
            if m.get("id") == cmid:
                return m
    raise MoodleError(f"Activity cmid {cmid} not found in course {course_id}.")


def _html_files(module: dict) -> list[dict]:
    return [
        f for f in module.get("contents") or []
        if f.get("type") == "file" and f.get("filename", "").lower().endswith(".html")
    ]


def _book_chapters(module: dict) -> list[dict]:
    """Order a Book's chapter files using its "structure" table of contents.

    Each chapter is exported as <chapterid>/index.html; the structure entry is a
    JSON tree of {title, href, level, subitems}. Falls back to file order.
    """
    files = {f"{f.get('filepath', '/').strip('/')}/{f['filename']}": f for f in _html_files(module)}
    structure = next(
        (c for c in module.get("contents") or [] if c.get("filename") == "structure"), None
    )
    ordered: list[dict] = []

    def walk(items: list) -> None:
        for item in items:
            f = files.pop(item.get("href", ""), None)
            if f:
                ordered.append({"title": _htmllib.unescape(item.get("title", "")), "file": f})
            walk(item.get("subitems") or [])

    if structure and structure.get("content"):
        try:
            walk(json.loads(structure["content"]))
        except ValueError:
            pass
    leftovers = sorted(files.values(), key=lambda f: f.get("sortorder") or 0)
    ordered.extend({"title": "", "file": f} for f in leftovers)
    return ordered


@tool()
async def read_activity(course_id: int, cmid: int) -> dict:
    """Read the full text of a Page, Book (all chapters) or Label.

    Read-only. `cmid` is the activity's course module id (from
    get_course_content). Downloads each HTML file the activity exposes and
    returns plain text, not HTML. Books return every chapter in table-of-
    contents order, both separately under `chapters` and joined under `text`.
    The web service must allow file downloads ("Can download files").
    """
    module = await _find_module(course_id, cmid)
    modname = module.get("modname")
    result: dict[str, Any] = {
        "course_id": course_id,
        "cmid": cmid,
        "modname": modname,
        "name": module.get("name"),
    }

    if modname == "label":
        # A label's content is returned inline as the module description.
        text = _html_to_text(module.get("description"))
        if not text and _html_files(module):
            async with httpx.AsyncClient(timeout=60.0) as client:
                parts = [await _download_text(client, f["fileurl"]) for f in _html_files(module)]
            text = "\n\n".join(_html_to_text(p) for p in parts)
        result["text"] = text
        return result

    if modname == "page":
        files = _html_files(module)
        if not files:
            raise MoodleError(
                f"Page {cmid} exposes no HTML file. Check that the web service "
                "allows file downloads and the token's user can view the page."
            )
        async with httpx.AsyncClient(timeout=60.0) as client:
            parts = await asyncio.gather(*(_download_text(client, f["fileurl"]) for f in files))
        result["text"] = "\n\n".join(_html_to_text(p) for p in parts)
        return result

    if modname == "book":
        chapters = _book_chapters(module)
        async with httpx.AsyncClient(timeout=60.0) as client:
            bodies = await asyncio.gather(
                *(_download_text(client, ch["file"]["fileurl"]) for ch in chapters)
            )
        result["chapters"] = [
            {"title": ch["title"], "text": _html_to_text(body)}
            for ch, body in zip(chapters, bodies)
        ]
        result["text"] = "\n\n".join(
            f"{c['title']}\n\n{c['text']}" if c["title"] else c["text"]
            for c in result["chapters"]
        )
        return result

    raise MoodleError(
        f"read_activity supports page, book and label; cmid {cmid} is a {modname!r}. "
        "Use read_lesson for lessons."
    )


@tool()
async def read_lesson(course_id: int, cmid: int) -> dict:
    """Read the text of every page in a Lesson activity, in lesson order.

    Read-only. `cmid` is the lesson's course module id. Uses Moodle's core
    lesson web services: mod_lesson_get_lessons_by_courses to resolve the
    lesson, then mod_lesson_get_pages. Returns the lesson intro and, for each
    page, its title, type (e.g. Content, Multichoice) and plain-text contents.
    Full page contents generally need a teacher/manager role on the course.
    """
    data = await _call("mod_lesson_get_lessons_by_courses", {"courseids": [course_id]})
    lesson = next(
        (l for l in data.get("lessons", []) if l.get("coursemodule") == cmid), None
    )
    if not lesson:
        raise MoodleError(f"Lesson cmid {cmid} not found in course {course_id}.")

    pages_data = await _call("mod_lesson_get_pages", {"lessonid": lesson["id"]})
    pages = {p["page"]["id"]: p["page"] for p in pages_data.get("pages", [])}

    # Pages form a linked list via prevpageid/nextpageid; follow it from the
    # first page, then append anything unreachable so nothing is dropped.
    ordered: list[dict] = []
    current = next((p for p in pages.values() if not p.get("prevpageid")), None)
    seen: set[int] = set()
    while current and current["id"] not in seen:
        seen.add(current["id"])
        ordered.append(current)
        current = pages.get(current.get("nextpageid"))
    ordered.extend(p for pid, p in sorted(pages.items()) if pid not in seen)

    return {
        "course_id": course_id,
        "cmid": cmid,
        "lessonid": lesson["id"],
        "name": lesson.get("name"),
        "intro": _html_to_text(lesson.get("intro")),
        "pages": [
            {
                "pageid": p["id"],
                "title": p.get("title"),
                "type": p.get("typestring"),
                "text": _html_to_text(p.get("contents")),
            }
            for p in ordered
        ],
    }


# --------------------------------------------------------------------------- #
# WRITE TOOLS (registered only when MOODLE_ALLOW_WRITE=true).
# --------------------------------------------------------------------------- #

def _safe_backup_name(filename: Any, courseid: int) -> str:
    """The backup filename Moodle returned, reduced to a bare file name.

    It comes from the server, so any directory part (including "..") is
    dropped and the file always lands in the current directory. Falls back
    to course_<id>.mbz if nothing usable is left.
    """
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in ("", ".", ".."):
        return f"course_{courseid}.mbz"
    return name


def _register_write_tools() -> None:
    """Register write tools. Called only when writes are enabled."""

    # --- Core writes ------------------------------------------------------- #

    @tool()
    async def create_course(
        fullname: str,
        shortname: str,
        categoryid: int = 1,
        summary: str = "",
        visible: int = 1,
    ) -> list:
        """⚠️ WRITES LIVE DATA. Create a new course.

        Creates a real course on the Moodle site. `shortname` must be unique.
        `categoryid` must be an existing category (1 is usually the default).
        Returns the new course id and shortname.
        """
        course = {
            "fullname": fullname,
            "shortname": shortname,
            "categoryid": categoryid,
            "summary": summary,
            "summaryformat": 1,
            "visible": visible,
        }
        return await _call("core_course_create_courses", {"courses": [course]})

    @tool()
    async def create_category(name: str, parent: int = 0, description: str = "") -> list:
        """⚠️ WRITES LIVE DATA. Create a course category.

        `parent` 0 = top level. Returns the new category id and name.
        """
        category = {
            "name": name,
            "parent": parent,
            "description": description,
            "descriptionformat": 1,
        }
        return await _call("core_course_create_categories", {"categories": [category]})

    @tool()
    async def create_user(
        username: str,
        password: str,
        firstname: str,
        lastname: str,
        email: str,
        auth: str = "manual",
    ) -> list:
        """⚠️ WRITES LIVE DATA. Create a new user account.

        Creates a real login on the site. Password must meet the site's policy.
        Returns the new user id and username.
        """
        user = {
            "username": username,
            "password": password,
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "auth": auth,
        }
        return await _call("core_user_create_users", {"users": [user]})

    @tool()
    async def enrol_user(userid: int, courseid: int, roleid: int = 5) -> None:
        """⚠️ WRITES LIVE DATA. Enrol a user in a course (manual enrolment).

        `roleid` defaults to 5 (Student). Common roles: 3=Teacher, 4=Non-editing
        teacher, 5=Student. Requires the manual enrolment method to be enabled
        in the course. Returns null on success.
        """
        enrolment = {"userid": userid, "courseid": courseid, "roleid": roleid}
        return await _call("enrol_manual_enrol_users", {"enrolments": [enrolment]})

    @tool()
    async def create_group(courseid: int, name: str, description: str = "") -> list:
        """⚠️ WRITES LIVE DATA. Create a group inside a course.

        Returns the new group id and name.
        """
        group = {
            "courseid": courseid,
            "name": name,
            "description": description,
            "descriptionformat": 1,
        }
        return await _call("core_group_create_groups", {"groups": [group]})

    @tool()
    async def upload_file(
        filename: str,
        filecontent_base64: str,
        contextid: int = 0,
        component: str = "user",
        filearea: str = "draft",
        itemid: int = 0,
        filepath: str = "/",
    ) -> Any:
        """⚠️ WRITES LIVE DATA. Upload a file into Moodle's file storage.

        `filecontent_base64` is the base64-encoded file bytes. By default the
        file lands in the calling user's draft area (component=user,
        filearea=draft) — the returned itemid can then be attached to an
        activity. Returns the stored file descriptor(s).
        """
        params = {
            "contextid": contextid or 0,
            "component": component,
            "filearea": filearea,
            "itemid": itemid,
            "filepath": filepath,
            "filename": filename,
            "filecontent": filecontent_base64,
        }
        return await _call("core_files_upload", params)

    # --- Plugin writes (local_mcpconnector) ----------------------------------- #

    @tool()
    async def create_page(
        courseid: int,
        name: str,
        content: str,
        section: int = 0,
        intro: str = "",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Page activity (HTML) in a course.

        Requires the local_mcpconnector plugin on the Moodle site. `content` is the
        page body (HTML). `section` is the section number and must already
        exist: the plugin does not create missing sections and fails with
        "sectionnotfound" (use create_section first).

        Returns {"cmid": int, "instanceid": int}.
        """
        return await _call(
            "local_mcpconnector_create_page",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "content": content,
                "intro": intro,
                "visible": visible,
            },
        )

    @tool()
    async def create_book(
        courseid: int,
        name: str,
        chaptertitle: str,
        chaptercontent: str,
        section: int = 0,
        intro: str = "",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Book activity plus its first chapter.

        Requires the local_mcpconnector plugin. `chaptercontent` is HTML.
        `section` is the section number and must already exist: the plugin
        does not create missing sections and fails with "sectionnotfound".

        Returns {"cmid": int, "instanceid": int, "chapterid": int}, where
        chapterid is the first chapter's id.
        """
        return await _call(
            "local_mcpconnector_create_book",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "chaptertitle": chaptertitle,
                "chaptercontent": chaptercontent,
                "visible": visible,
            },
        )

    @tool()
    async def create_label(
        courseid: int,
        content: str,
        section: int = 0,
        name: str = "",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Label (inline text/HTML) in a course.

        Requires the local_mcpconnector plugin. Leave `name` empty to have one
        built from the label's text. `section` is the section number and must
        already exist: the plugin does not create missing sections and fails
        with "sectionnotfound".

        Returns {"cmid": int, "instanceid": int}.
        """
        return await _call(
            "local_mcpconnector_create_label",
            {
                "courseid": courseid,
                "section": section,
                "content": content,
                "name": name,
                "visible": visible,
            },
        )

    @tool()
    async def create_url(
        courseid: int,
        name: str,
        externalurl: str,
        section: int = 0,
        intro: str = "",
        display: int = 0,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a URL resource in a course.

        Requires the local_mcpconnector plugin. `display` must be a display
        mode enabled in the site's URL settings; 0 (automatic) is the safe
        default. `section` is the section number and must already exist: the
        plugin does not create missing sections and fails with
        "sectionnotfound".

        Returns {"cmid": int, "instanceid": int}.
        """
        return await _call(
            "local_mcpconnector_create_url",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "externalurl": externalurl,
                "intro": intro,
                "display": display,
                "visible": visible,
            },
        )

    @tool()
    async def create_quiz(
        courseid: int,
        name: str,
        section: int = 0,
        intro: str = "",
        timeopen: int = 0,
        timeclose: int = 0,
        timelimit: int = 0,
        grade: float = 100.0,
        attempts: int = 0,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create an empty Quiz activity (settings only).

        Requires the local_mcpconnector plugin.

        This creates the quiz container; add questions with the add_*_question
        tools. `timeopen` and `timeclose` are unix timestamps; `timeclose` must not
        be before `timeopen`. Settings this tool has no parameter for take the
        site's quiz defaults. `section` must already exist ("sectionnotfound"
        otherwise).

        Returns {"cmid": int, "instanceid": int}.
        """
        return await _call(
            "local_mcpconnector_create_quiz",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "timeopen": timeopen,
                "timeclose": timeclose,
                "timelimit": timelimit,
                "grade": grade,
                "attempts": attempts,
                "visible": visible,
            },
        )

    @tool()
    async def add_quiz_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        answers: list[dict],
        single: int = 1,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a multiple-choice question to an existing quiz.

        Requires the local_mcpconnector plugin.

        `quizcmid` is the quiz's course module id (the cmid returned by
        create_quiz). `answers` is a list of at least two dicts, each:
        {"text": "...", "fraction": 1.0, "feedback": ""}, where fraction is the
        share of the mark the answer earns; answers without "fraction" are worth 0.
        With `single=1` (one correct answer) one answer must have fraction 1.0.
        With `single=0` (several correct answers) the positive fractions must add
        up to 1.0.

        Fractions must be Moodle grade values: 1.0, 0.9, 0.8333333, 0.8, 0.75, 0.7,
        0.6666667, 0.6, 0.5, 0.4, 0.3333333, 0.3, 0.25, 0.2, 0.1666667, 0.1428571,
        0.125, 0.1111111, 0.1, 0.05, 0, or their negatives. Values within 0.005 of
        one are accepted (0.33 counts as a third).

        The question is added at the end of the quiz, in the quiz's own question
        bank. Adding fails with "cannoteditafterattempts" once students have
        attempted the quiz.

        Returns {"questionid": int, "slot": int}.
        """
        _require_list("answers", answers, 2)
        return await _call(
            "local_mcpconnector_add_quiz_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "answers": answers,
                "single": single,
                "defaultmark": defaultmark,
            },
        )

    @tool()
    async def add_truefalse_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        correctanswer: int,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a true/false question to a quiz.

        Requires the local_mcpconnector plugin.

        `quizcmid` is the quiz's course module id. `correctanswer` = 1 for true,
        0 for false.

        The question is added at the end of the quiz, in the quiz's own question
        bank. Adding fails with "cannoteditafterattempts" once students have
        attempted the quiz.

        Returns {"questionid": int, "slot": int}.
        """
        return await _call(
            "local_mcpconnector_add_truefalse_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "correctanswer": correctanswer,
                "defaultmark": defaultmark,
            },
        )

    @tool()
    async def add_shortanswer_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        answers: list[str],
        usecase: int = 0,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a short-answer question to a quiz.

        Requires the local_mcpconnector plugin.

        `answers` is a list of at least one accepted answer string (all treated as
        fully correct). `usecase` = 1 for case-sensitive matching.

        The question is added at the end of the quiz, in the quiz's own question
        bank. Adding fails with "cannoteditafterattempts" once students have
        attempted the quiz.

        Returns {"questionid": int, "slot": int}.
        """
        _require_list("answers", answers)
        return await _call(
            "local_mcpconnector_add_shortanswer_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "answers": answers,
                "usecase": usecase,
                "defaultmark": defaultmark,
            },
        )

    @tool()
    async def add_essay_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        graderinfo: str = "",
        responsetemplate: str = "",
        responselines: int = 15,
        attachments: int = 0,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add an essay (free-text) question to a quiz.

        Requires the local_mcpconnector plugin.

        Essay questions are graded manually. `graderinfo` is private guidance for
        whoever marks it; `responsetemplate` pre-fills the student's answer box.
        `responselines` (the answer box height) must be one of 2, 3, 5, 10, 15,
        20, 25, 30, 35, 40. `attachments` (files the student may upload) must be
        one of 0, 1, 2, 3, or -1 for unlimited.

        The question is added at the end of the quiz, in the quiz's own question
        bank. Adding fails with "cannoteditafterattempts" once students have
        attempted the quiz.

        Returns {"questionid": int, "slot": int}.
        """
        return await _call(
            "local_mcpconnector_add_essay_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "graderinfo": graderinfo,
                "responsetemplate": responsetemplate,
                "responselines": responselines,
                "attachments": attachments,
                "defaultmark": defaultmark,
            },
        )

    @tool()
    async def add_numerical_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        answers: list[dict],
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a numerical question to a quiz.

        Requires the local_mcpconnector plugin.

        `answers` is a list of at least one dict, each: {"answer": 42.0,
        "tolerance": 0.5, "fraction": 1.0, "feedback": ""}. A student response
        within ±tolerance of answer earns that fraction of the mark. One answer
        must have fraction 1.0 (the default when "fraction" is left out), and
        tolerance must be 0 or more.

        Fractions must be Moodle grade values: 1.0, 0.9, 0.8333333, 0.8, 0.75, 0.7,
        0.6666667, 0.6, 0.5, 0.4, 0.3333333, 0.3, 0.25, 0.2, 0.1666667, 0.1428571,
        0.125, 0.1111111, 0.1, 0.05, 0, or their negatives. Values within 0.005 of
        one are accepted (0.33 counts as a third).

        The question is added at the end of the quiz, in the quiz's own question
        bank. Adding fails with "cannoteditafterattempts" once students have
        attempted the quiz.

        Returns {"questionid": int, "slot": int}.
        """
        _require_list("answers", answers)
        return await _call(
            "local_mcpconnector_add_numerical_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "answers": answers,
                "defaultmark": defaultmark,
            },
        )

    @tool()
    async def add_matching_question(
        quizcmid: int,
        name: str,
        questiontext: str,
        pairs: list[dict],
        shuffleanswers: int = 1,
        defaultmark: float = 1.0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Add a matching question to a quiz.

        Requires the local_mcpconnector plugin.

        `pairs` is a list of dicts, each: {"question": "Capital of France",
        "answer": "Paris"}. Students match each left-hand item to the right
        answer. At least three pairs: Moodle requires at least two questions and
        three answers.

        The question is added at the end of the quiz, in the quiz's own question
        bank. Adding fails with "cannoteditafterattempts" once students have
        attempted the quiz.

        Returns {"questionid": int, "slot": int}.
        """
        _require_list("pairs", pairs, 3)
        return await _call(
            "local_mcpconnector_add_matching_question",
            {
                "quizcmid": quizcmid,
                "name": name,
                "questiontext": questiontext,
                "pairs": pairs,
                "shuffleanswers": shuffleanswers,
                "defaultmark": defaultmark,
            },
        )

    @tool()
    async def enrol_users(enrolments: list[dict]) -> None:
        """⚠️ WRITES LIVE DATA. Enrol many users at once (bulk).

        `enrolments` is a list of dicts, each: {"userid": int, "courseid": int,
        "roleid": int}. roleid 5 = Student, 3 = Teacher, 4 = Non-editing teacher.
        Requires the manual enrolment method enabled on each course. Returns null
        on success.
        """
        _require_list("enrolments", enrolments)
        return await _call("enrol_manual_enrol_users", {"enrolments": enrolments})

    @tool()
    async def add_group_members(members: list[dict]) -> None:
        """⚠️ WRITES LIVE DATA. Add users to groups.

        `members` is a list of dicts, each: {"groupid": int, "userid": int}.
        The users must already be enrolled in the group's course. Returns null
        on success.
        """
        _require_list("members", members)
        return await _call("core_group_add_group_members", {"members": members})

    @tool()
    async def create_forum(
        courseid: int,
        name: str,
        section: int = 0,
        intro: str = "",
        type: str = "general",
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Forum activity in a course.

        Requires the local_mcpconnector plugin.

        `type` is one of: general, eachuser, single, qanda, blog. Settings this
        tool has no parameter for take the site's forum defaults. `section` must
        already exist ("sectionnotfound" otherwise).

        Returns {"cmid": int, "instanceid": int}.
        """
        return await _call(
            "local_mcpconnector_create_forum",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "type": type,
                "visible": visible,
            },
        )

    @tool()
    async def create_choice(
        courseid: int,
        name: str,
        question: str,
        options: list[str],
        section: int = 0,
        allowupdate: int = 1,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a Choice (poll) activity in a course.

        Requires the local_mcpconnector plugin.

        `question` becomes the choice's description, shown above the options.
        `options` is a list of at least two answer strings students pick from.
        `section` must already exist ("sectionnotfound" otherwise).

        Returns {"cmid": int, "instanceid": int}.
        """
        _require_list("options", options, 2)
        return await _call(
            "local_mcpconnector_create_choice",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "question": question,
                "options": options,
                "allowupdate": allowupdate,
                "visible": visible,
            },
        )

    @tool()
    async def create_assignment(
        courseid: int,
        name: str,
        section: int = 0,
        intro: str = "",
        duedate: int = 0,
        grade: int = 100,
        visible: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create an Assignment activity in a course.

        Requires the local_mcpconnector plugin.

        Enables online-text and file submissions with feedback comments.
        `duedate` is a unix timestamp (0 = no due date). `grade` must be between
        0 and the site's maximum grade (100 by default). Settings this tool has no
        parameter for take the site's assignment defaults. `section` must already
        exist ("sectionnotfound" otherwise).

        Returns {"cmid": int, "instanceid": int}.
        """
        return await _call(
            "local_mcpconnector_create_assignment",
            {
                "courseid": courseid,
                "section": section,
                "name": name,
                "intro": intro,
                "duedate": duedate,
                "grade": grade,
                "visible": visible,
            },
        )

    @tool()
    async def create_section(
        courseid: int,
        name: str,
        summary: str = "",
        position: int = 0,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Create a new section (topic) in a course.

        Requires the local_mcpconnector plugin. `position` 0 appends to the end.

        Returns {"sectionid": int, "sectionnum": int}: the new section's id
        and its number within the course (the `section` value other tools
        take).
        """
        return await _call(
            "local_mcpconnector_create_section",
            {
                "courseid": courseid,
                "name": name,
                "summary": summary,
                "position": position,
            },
        )

    @tool()
    async def update_activity(cmid: int, name: str = "", visible: int = -1) -> dict:
        """⚠️ WRITES LIVE DATA. Rename and/or show/hide an existing activity.

        Requires the local_mcpconnector plugin. `cmid` is the activity's course
        module id. Leave `name` empty to keep it; `visible` = 1 show, 0 hide,
        -1 leave unchanged.

        Returns {"cmid": int, "name": str, "visible": int, "changed": [...]},
        with the activity's resulting name and visibility; `changed` lists
        which of "name" and "visible" were actually modified.
        """
        return await _call(
            "local_mcpconnector_update_activity",
            {"cmid": cmid, "name": name, "visible": visible},
        )

    @tool()
    async def update_page(cmid: int, content: str, intro: str = "") -> dict:
        """⚠️ WRITES LIVE DATA. Replace the content of an existing Page activity.

        Requires the local_mcpconnector plugin. `cmid` is the page's course module
        id; `content` is the new HTML body. Leave `intro` empty to keep the
        current description.

        Returns {"cmid": int, "success": bool}.
        """
        return await _call(
            "local_mcpconnector_update_page",
            {"cmid": cmid, "content": content, "intro": intro},
        )

    @tool()
    async def move_activity(cmid: int, section: int) -> dict:
        """⚠️ WRITES LIVE DATA. Move an activity to a different course section.

        Requires the local_mcpconnector plugin. `cmid` is the activity's course
        module id; `section` is the target section number, which must already
        exist (the plugin fails with "sectionnotfound" otherwise). The activity
        is placed at the END of the target section.

        Returns {"cmid": int, "section": int}: the section number it now
        lives in.
        """
        return await _call(
            "local_mcpconnector_move_activity",
            {"cmid": cmid, "section": section},
        )

    @tool()
    async def update_course(
        courseid: int,
        fullname: str = "",
        shortname: str = "",
        summary: str = "",
        visible: int = -1,
    ) -> list:
        """⚠️ WRITES LIVE DATA. Edit a course's settings.

        Only the fields you provide are changed: leave `fullname`/`shortname`/
        `summary` empty to keep them, and `visible` = 1 show, 0 hide, -1 leave.
        Returns the update result.
        """
        course: dict[str, Any] = {"id": courseid}
        if fullname:
            course["fullname"] = fullname
        if shortname:
            course["shortname"] = shortname
        if summary:
            course["summary"] = summary
            course["summaryformat"] = 1
        if visible in (0, 1):
            course["visible"] = visible
        return await _call("core_course_update_courses", {"courses": [course]})

    @tool()
    async def delete_activity(cmid: int) -> dict:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete an activity from a course.

        Requires the local_mcpconnector plugin. `cmid` is the activity's course
        module id. This permanently removes the activity and its data.

        Returns {"cmid": int, "success": bool}.
        """
        return await _call("local_mcpconnector_delete_activity", {"cmid": cmid})

    @tool()
    async def grade_assignment(
        assignmentid: int,
        userid: int,
        grade: float,
        feedback: str = "",
        attemptnumber: int = -1,
        applytoall: int = 1,
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Grade a student's assignment submission.

        Uses Moodle's core mod_assign_save_grade. `assignmentid` is the assign
        instance id (NOT the cmid — get it from get_assignments/get_course_content).
        `grade` is the numeric mark (e.g. 85 for 85/100); pass -1 for "no grade".
        `feedback` is optional HTML shown to the student as a feedback comment.
        `attemptnumber` -1 targets the latest attempt. The grade flows straight
        to the gradebook. Returns a summary of what was written.
        """
        params: dict[str, Any] = {
            "assignmentid": assignmentid,
            "userid": userid,
            "grade": grade,
            "attemptnumber": attemptnumber,
            "addattempt": 0,
            "workflowstate": "",
            "applytoall": applytoall,
        }
        if feedback:
            params["plugindata"] = {
                "assignfeedbackcomments_editor": {"text": feedback, "format": 1}
            }
        # mod_assign_save_grade returns null on success.
        await _call("mod_assign_save_grade", params)
        return {
            "status": "graded",
            "assignmentid": assignmentid,
            "userid": userid,
            "grade": grade,
            "feedback": feedback or None,
        }

    @tool()
    async def unenrol_user(userid: int, courseid: int) -> None:
        """⚠️ WRITES LIVE DATA. Remove a user's manual enrolment from a course.

        Uses Moodle's core enrol_manual_unenrol_users. Only removes the manual
        enrolment (other enrolment methods are untouched). Returns null on
        success.
        """
        return await _call(
            "enrol_manual_unenrol_users",
            {"enrolments": [{"userid": userid, "courseid": courseid}]},
        )

    @tool()
    async def delete_section(courseid: int, sectionnum: int) -> dict:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete a course section.

        Requires the local_mcpconnector plugin. `sectionnum` is the section number
        (1 or higher — the general section 0 cannot be deleted). This removes the
        section AND every activity inside it.

        Returns {"success": bool}.
        """
        return await _call(
            "local_mcpconnector_delete_section",
            {"courseid": courseid, "sectionnum": sectionnum},
        )

    # --- Extended core writes (Moodle core web services) ------------------- #

    @tool()
    async def delete_courses(courseids: list[int]) -> dict:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete one or more courses.

        Uses core_course_delete_courses. Every course and all its content is
        permanently removed. Pass a list of course ids. Returns Moodle's
        warnings list (empty when everything deleted cleanly).
        """
        _require_list("courseids", courseids)
        return await _call("core_course_delete_courses", {"courseids": courseids})

    @tool()
    async def delete_category(categoryid: int, recursive: int = 0) -> None:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete a course category.

        Uses core_course_delete_categories. `recursive` 1 also deletes the
        category's courses and sub-categories; 0 fails if it is not empty.
        """
        cat = {"id": categoryid, "recursive": recursive}
        return await _call("core_course_delete_categories", {"categories": [cat]})

    @tool()
    async def update_user(
        userid: int,
        firstname: str = "",
        lastname: str = "",
        email: str = "",
        suspended: int = -1,
    ) -> None:
        """⚠️ WRITES LIVE DATA. Update an existing user (incl. suspend).

        Uses core_user_update_users. Only non-empty fields are sent. Set
        `suspended` to 1 to disable the account or 0 to re-enable it (leave -1
        to keep it unchanged).
        """
        user: dict[str, Any] = {"id": userid}
        if firstname:
            user["firstname"] = firstname
        if lastname:
            user["lastname"] = lastname
        if email:
            user["email"] = email
        if suspended in (0, 1):
            user["suspended"] = suspended
        return await _call("core_user_update_users", {"users": [user]})

    @tool()
    async def delete_users(userids: list[int]) -> None:
        """⚠️ WRITES LIVE DATA — IRREVERSIBLE. Delete user accounts.

        Uses core_user_delete_users. Pass a list of user ids.
        """
        _require_list("userids", userids)
        return await _call("core_user_delete_users", {"userids": userids})

    @tool()
    async def assign_role(
        roleid: int, userid: int, contextlevel: str = "course", instanceid: int = 0
    ) -> None:
        """⚠️ WRITES LIVE DATA. Assign a role to a user in a context.

        Uses core_role_assign_roles. `contextlevel` is usually "course" (with
        `instanceid` = course id), "coursecat", "user", or "system". Common
        role ids: 3 editingteacher, 4 teacher, 5 student, 1 manager.
        """
        a = {
            "roleid": roleid,
            "userid": userid,
            "contextlevel": contextlevel,
            "instanceid": instanceid,
        }
        return await _call("core_role_assign_roles", {"assignments": [a]})

    @tool()
    async def unassign_role(
        roleid: int, userid: int, contextlevel: str = "course", instanceid: int = 0
    ) -> None:
        """⚠️ WRITES LIVE DATA. Remove a role from a user in a context.

        Uses core_role_unassign_roles. Same arguments as assign_role.
        """
        a = {
            "roleid": roleid,
            "userid": userid,
            "contextlevel": contextlevel,
            "instanceid": instanceid,
        }
        return await _call("core_role_unassign_roles", {"unassignments": [a]})

    @tool()
    async def create_cohort(
        name: str, idnumber: str, categoryid: int = 0, description: str = ""
    ) -> list:
        """⚠️ WRITES LIVE DATA. Create a cohort (site-wide user group).

        Uses core_cohort_create_cohorts. `categoryid` 0 = system context.
        Returns the new cohort id.
        """
        ctxid = categoryid or 1
        cohort = {
            "categorytype": {"type": "id", "value": str(ctxid)},
            "name": name,
            "idnumber": idnumber,
            "description": description,
        }
        return await _call("core_cohort_create_cohorts", {"cohorts": [cohort]})

    @tool()
    async def add_cohort_members(cohortid: int, userids: list[int]) -> dict:
        """⚠️ WRITES LIVE DATA. Add users to a cohort.

        Uses core_cohort_add_cohort_members.
        """
        _require_list("userids", userids)
        members = [
            {
                "cohorttype": {"type": "id", "value": str(cohortid)},
                "usertype": {"type": "id", "value": str(uid)},
            }
            for uid in userids
        ]
        return await _call("core_cohort_add_cohort_members", {"members": members})

    @tool()
    async def start_forum_discussion(
        forumid: int, subject: str, message: str
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Start a new forum discussion (topic).

        Uses mod_forum_add_discussion. `forumid` is the forum instance id.
        Returns the new discussion id.
        """
        return await _call(
            "mod_forum_add_discussion",
            {"forumid": forumid, "subject": subject, "message": message},
        )

    @tool()
    async def reply_forum_post(postid: int, subject: str, message: str) -> dict:
        """⚠️ WRITES LIVE DATA. Reply to a forum post.

        Uses mod_forum_add_discussion_post. `postid` is the parent post id (the
        discussion's first post to reply at the top level). Returns the new post id.
        """
        return await _call(
            "mod_forum_add_discussion_post",
            {"postid": postid, "subject": subject, "message": message},
        )

    @tool()
    async def send_message(touserid: int, text: str) -> list:
        """⚠️ WRITES LIVE DATA. Send a private message to a user.

        Uses core_message_send_instant_messages. Requires messaging enabled and
        the moodle/site:sendmessage capability.
        """
        msg = {"touserid": touserid, "text": text, "textformat": 1}
        return await _call(
            "core_message_send_instant_messages", {"messages": [msg]}
        )

    @tool()
    async def import_course(importfrom: int, importto: int, deletecontent: int = 0) -> None:
        """⚠️ WRITES LIVE DATA. Import content from one course into another.

        Uses core_course_import_course. Copies activities/resources from
        `importfrom` into `importto`. `deletecontent` 1 wipes the target first.
        """
        return await _call(
            "core_course_import_course",
            {"importfrom": importfrom, "importto": importto, "deletecontent": deletecontent},
        )

    @tool()
    async def download_file(fileurl: str) -> dict:
        """Download a file from the Moodle site by its pluginfile URL.

        Requires the service to allow file downloads (downloadfiles=1).
        `fileurl` must be on the configured MOODLE_URL site (e.g. a fileurl
        from get_course_content); any other URL is refused, so the token is
        never sent elsewhere. Appends the token, fetches the bytes, and
        returns the size and base64 content.
        """
        import base64 as _b64

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await _fetch_file(client, fileurl)
            content = resp.content
        return {
            "size": len(content),
            "content_type": resp.headers.get("content-type", ""),
            "content_base64": _b64.b64encode(content).decode("ascii"),
        }

    @tool()
    async def mark_activity_complete(cmid: int, completed: bool = True) -> dict:
        """⚠️ WRITES LIVE DATA. Tick (or untick) your own manual completion box.

        Works only on activities set to manual completion tracking, and only
        for the current user. Use override_activity_completion to set another
        student's state as a teacher.
        """
        await _call(
            "core_completion_update_activity_completion_status_manually",
            {"cmid": cmid, "completed": completed},
        )
        return {"cmid": cmid, "completed": completed}

    @tool()
    async def override_activity_completion(
        cmid: int, userid: int, newstate: int = 1
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Override a student's activity completion state.

        Teacher action. `newstate`: 0 = incomplete, 1 = complete. The override
        is recorded against the acting user in Moodle's completion log.
        """
        return await _call(
            "core_completion_override_activity_completion_status",
            {"cmid": cmid, "userid": userid, "newstate": newstate},
        )

    @tool()
    async def backup_course(
        courseid: int, save_to: str = "", includeusers: int = 0
    ) -> dict:
        """Back a whole course up to a .mbz file on the local machine.

        Requires the local_mcpconnector plugin.

        The backup runs on the Moodle server and the plugin returns
        {"filename": str, "size": int, "content_base64": str}, where filename is
        like backup-moodle2-course-12-bio101-20261021-1200-nu.mbz. This tool
        decodes it and writes it to `save_to`, or, when `save_to` is empty,
        under that filename in the current directory.
        Content-only by default. includeusers=1 keeps enrolments and user data,
        but needs the moodle/backup:userinfo capability, which managers have and
        teachers do not by default. Backups over 100 MB are refused.

        Returns {"saved_to": str, "size": int, "filename": str}: the local path
        written, the size in bytes, and Moodle's name for the backup.
        """
        import base64 as _b64

        result = await _call(
            "local_mcpconnector_backup_course",
            {"courseid": courseid, "includeusers": includeusers},
        )
        path = save_to or _safe_backup_name(result.get("filename"), courseid)
        await asyncio.to_thread(Path(path).write_bytes, _b64.b64decode(result["content_base64"]))
        return {
            "saved_to": os.path.abspath(path),
            "size": result["size"],
            "filename": result["filename"],
        }

    @tool()
    async def restore_course(
        filepath: str,
        categoryid: int = 1,
        fullname: str = "",
        shortname: str = "",
    ) -> dict:
        """⚠️ WRITES LIVE DATA. Restore a .mbz backup file into a NEW course.

        Requires the local_mcpconnector plugin.

        Reads the local `filepath` (created by backup_course, or any Moodle .mbz),
        uploads it base64-encoded and restores it as a brand-new course in
        `categoryid`; it never overwrites an existing course. `fullname` and
        `shortname` override the names stored in the backup. If a name clashes
        with an existing course, Moodle adds a " copy 1" (full name) or "_1"
        (short name) suffix, so report the names from the result, not the ones
        requested.

        The token's user needs moodle/course:create, moodle/restore:restorecourse
        and moodle/restore:uploadfile in the target category (a category manager,
        not a teacher). The web server's post_max_size must allow the base64
        upload, about 4/3 of the .mbz size.

        Returns {"courseid": int, "fullname": str, "shortname": str}.
        """
        import base64 as _b64

        content = await asyncio.to_thread(Path(filepath).read_bytes)
        return await _call(
            "local_mcpconnector_restore_course",
            {
                "content_base64": _b64.b64encode(content).decode("ascii"),
                "categoryid": categoryid,
                "fullname": fullname,
                "shortname": shortname,
            },
        )

    @tool()
    async def build_course(outline: dict, rollback: bool = False) -> dict:
        """⚠️ WRITES LIVE DATA. Build a whole course from one JSON outline.

        The outline is validated up front — a malformed one is rejected before
        anything is written, with a list of problems. It is also checked
        against the site's function list: if the plugin (or the token's
        service) lacks a function the outline needs, such as quizzes, the
        outline is rejected before anything is written, naming what is
        missing. Set `rollback=True` to
        make the build transactional: if any section or activity fails AND this
        call created the course, the whole course is deleted so you are not left
        with a half-built one. With `rollback=False` (default) a failing item is
        recorded in the log and the rest still build.

        One call instead of many. `outline` shape:
        {
          "fullname": "Biology 101", "shortname": "bio101", "categoryid": 1,
          "summary": "...",            # optional
          "courseid": 0,               # optional: build into an existing course
          "sections": [
            {"name": "Week 1", "summary": "...",
             "activities": [
               {"type": "page", "name": "Welcome", "content": "<p>Hi</p>"},
               {"type": "label", "text": "<b>Read this first</b>"},
               {"type": "url", "name": "Syllabus", "url": "https://..."},
               {"type": "forum", "name": "Q&A", "intro": "..."},
               {"type": "assignment", "name": "Report", "intro": "...",
                "duedate": 0},
               {"type": "quiz", "name": "Checkpoint", "intro": "...",
                "questions": [
                  {"qtype": "multichoice", "name": "Q1", "text": "...",
                   "answers": [{"text": "A", "fraction": 1.0},
                                {"text": "B", "fraction": 0.0}]},
                  {"qtype": "truefalse", "name": "Q2", "text": "...",
                   "correct": 1},
                  {"qtype": "shortanswer", "name": "Q3", "text": "...",
                   "answers": ["Paris"]},
                  {"qtype": "essay", "name": "Q4", "text": "..."},
                  {"qtype": "numerical", "name": "Q5", "text": "...",
                   "answers": [{"answer": 42, "tolerance": 0.5}]},
                  {"qtype": "matching", "name": "Q6", "text": "...",
                   "pairs": [{"question": "France", "answer": "Paris"},
                              {"question": "Italy", "answer": "Rome"},
                              {"question": "Spain", "answer": "Madrid"}]}
                ]}
             ]}
          ]
        }
        Creates the course (unless `courseid` is given), then every section
        and activity in order. Returns the course id plus a per-item build
        log; one failing activity is reported but does not stop the rest.
        """
        problems = _validate_outline(outline)
        if problems:
            raise MoodleError("Invalid course outline: " + "; ".join(problems))

        # Check every function the build needs before writing anything, so a
        # site whose plugin lacks e.g. quizzes rejects the outline up front
        # instead of failing halfway through.
        available = await _available_functions()
        missing = [
            _missing_function_message(what, fn)
            for what, fn in _outline_functions(outline, rollback)
            if fn not in available
        ]
        if missing:
            raise MoodleError("Cannot build this course: " + " ".join(missing))

        log: list[dict] = []
        created_course = False

        courseid = int(outline.get("courseid") or 0)
        if not courseid:
            created_course = True
            created = await _call(
                "core_course_create_courses",
                {
                    "courses": [
                        {
                            "fullname": outline["fullname"],
                            "shortname": outline["shortname"],
                            "categoryid": outline.get("categoryid", 1),
                            "summary": outline.get("summary", ""),
                            "summaryformat": 1,
                            "format": "topics",
                        }
                    ]
                },
            )
            courseid = created[0]["id"]
            log.append({"created": "course", "id": courseid})

        async def build_activity(sectionnum: int, act: dict) -> None:
            atype = act.get("type", "")
            name = act.get("name", "")
            if atype == "page":
                r = await _call(
                    "local_mcpconnector_create_page",
                    {
                        "courseid": courseid,
                        "name": name,
                        "content": act.get("content", ""),
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                    },
                )
            elif atype == "label":
                r = await _call(
                    "local_mcpconnector_create_label",
                    {
                        "courseid": courseid,
                        "content": act.get("text", act.get("content", "")),
                        "section": sectionnum,
                    },
                )
            elif atype == "url":
                r = await _call(
                    "local_mcpconnector_create_url",
                    {
                        "courseid": courseid,
                        "name": name,
                        "externalurl": act.get("url", ""),
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                    },
                )
            elif atype == "forum":
                r = await _call(
                    "local_mcpconnector_create_forum",
                    {
                        "courseid": courseid,
                        "name": name,
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                        "type": act.get("forumtype", "general"),
                    },
                )
            elif atype == "assignment":
                r = await _call(
                    "local_mcpconnector_create_assignment",
                    {
                        "courseid": courseid,
                        "name": name,
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                        "duedate": act.get("duedate", 0),
                        "grade": act.get("grade", 100),
                    },
                )
            elif atype == "choice":
                r = await _call(
                    "local_mcpconnector_create_choice",
                    {
                        "courseid": courseid,
                        "name": name,
                        "question": act.get("question", name),
                        "options": act.get("options", []),
                        "section": sectionnum,
                    },
                )
            elif atype == "quiz":
                r = await _call(
                    "local_mcpconnector_create_quiz",
                    {
                        "courseid": courseid,
                        "name": name,
                        "section": sectionnum,
                        "intro": act.get("intro", ""),
                    },
                )
                quizcmid = r.get("cmid")
                for q in act.get("questions", []):
                    await build_question(quizcmid, q)
            else:
                raise MoodleError(f"Unknown activity type: {atype!r}")
            log.append({"created": atype or "?", "name": name, "result": r})

        async def build_question(quizcmid: int, q: dict) -> None:
            qtype = q.get("qtype", "multichoice")
            common = {
                "quizcmid": quizcmid,
                "name": q.get("name", "Question"),
                "questiontext": q.get("text", ""),
                "defaultmark": q.get("defaultmark", 1.0),
            }
            if qtype == "multichoice":
                await _call(
                    "local_mcpconnector_add_quiz_question",
                    {**common, "answers": q.get("answers", []),
                     "single": q.get("single", 1)},
                )
            elif qtype == "truefalse":
                await _call(
                    "local_mcpconnector_add_truefalse_question",
                    {**common, "correctanswer": q.get("correct", 1)},
                )
            elif qtype == "shortanswer":
                await _call(
                    "local_mcpconnector_add_shortanswer_question",
                    {**common, "answers": q.get("answers", []),
                     "usecase": q.get("usecase", 0)},
                )
            elif qtype == "essay":
                await _call(
                    "local_mcpconnector_add_essay_question",
                    {**common, "graderinfo": q.get("graderinfo", "")},
                )
            elif qtype == "numerical":
                await _call(
                    "local_mcpconnector_add_numerical_question",
                    {**common, "answers": q.get("answers", [])},
                )
            elif qtype == "matching":
                await _call(
                    "local_mcpconnector_add_matching_question",
                    {**common, "pairs": q.get("pairs", [])},
                )
            else:
                raise MoodleError(f"Unknown question type: {qtype!r}")

        async def do_build() -> None:
            for i, sec in enumerate(outline.get("sections", []), start=1):
                r = await _call(
                    "local_mcpconnector_create_section",
                    {
                        "courseid": courseid,
                        "name": sec.get("name", f"Section {i}"),
                        "summary": sec.get("summary", ""),
                    },
                )
                sectionnum = r.get("sectionnum", i)
                log.append({"created": "section", "name": sec.get("name"),
                            "sectionnum": sectionnum})
                for act in sec.get("activities", []):
                    try:
                        await build_activity(sectionnum, act)
                    except (MoodleError, httpx.HTTPError) as exc:
                        entry = {
                            "error": str(exc),
                            "activity": act.get("name", act.get("type", "?")),
                        }
                        log.append(entry)
                        if rollback:
                            raise MoodleError(entry["error"]) from exc

        if rollback:
            try:
                await do_build()
            except (MoodleError, httpx.HTTPError) as exc:
                if created_course:
                    try:
                        await _call("core_course_delete_courses",
                                    {"courseids": [courseid]})
                    except (MoodleError, httpx.HTTPError):
                        pass  # best-effort cleanup; surface the original failure
                raise MoodleError(
                    f"build_course rolled back (course {'deleted' if created_course else 'kept'}): "
                    f"{exc}"
                ) from exc
        else:
            await do_build()

        return {"courseid": courseid, "built": log,
                "rolledback": False, "ok": not any("error" in e for e in log)}


if ALLOW_WRITE:
    _register_write_tools()


def main() -> None:
    """Entry point for the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
