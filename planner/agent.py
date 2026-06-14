from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from planner.db import EventStore, week_start

SYSTEM_PROMPT = """You are the user's personal scheduling assistant inside a terminal planner app.

You have tools to read and modify the user's calendar, projects, tasks, and
time-tracking sessions in a local SQLite database. Always confirm what you
changed in plain language after using a tool.

Datetime format: ISO 8601 local time, e.g. "2026-06-14T09:30".
When the user uses relative terms ("tomorrow", "next Tuesday", "in 2 hours"),
resolve them against the provided "now" context and pass concrete ISO strings to tools.

# Two parallel concepts

The user has TWO separate concepts; pick the right one:

- **Events** — calendar appointments / obligations with a fixed time (meetings,
  flights, gym, birthdays). Use `add_event`, `add_recurring`, etc.
- **Tasks** — work items grouped under projects. May or may not have a planned
  time. Use `add_task`. Track work time with `start_session` / `stop_session`.

Rules of thumb:
- "I have a meeting at 3" → add_event
- "I need to write the blog post by Friday" → add_task (with scheduled_for)
- "Add 'review PR' to my planner-app project" → add_task
- "Start working on the API redesign" → start_session
- "I worked on X yesterday from 2 to 4" → add_session (backfill)
- "Mark task #5 done" → complete_task
- "What's on my plate today?" → list_events + list_tasks (scheduled_on=today)

# Event metadata

Every event has tags / all_day / priority — set them thoughtfully (1–3 short
lowercase tags inferred from title). Birthdays/holidays → all_day=true,
priority=low. Use `find_conflicts` before adding a NORMAL-priority timed event.

# Recurring events

`add_recurring` with daily/weekly/monthly/yearly. Pick a sensible `until` if
the user doesn't (yearly: 5y, weekly/daily: end of year).
Use `delete_series` to remove a whole recurring series.

# Projects and tasks

Tasks always belong to a project. If the user names a project that doesn't
exist, create it with `add_project` first (or use `ensure_project` semantics:
list_projects first, create if missing).

If the user says "quick start X" or "I'm working on X" without a project, put
the task under a project called "Inbox" (create if missing). Don't pester
them about picking a project for ad-hoc work.

Task fields:
- `scheduled_for`: optional ISO datetime — when the user plans to do it.
  Shows in the agenda view for that day.
- `estimate_minutes`: optional time estimate.

# Sessions

A session is a tracked interval on a task. `start_session(task_id)` opens one
at now. `stop_session(session_id)` closes it. Only suggest stopping a session
if the user signals they're done.

For backfilling ("I worked on X from 9 to 11 this morning"), use `add_session`
with explicit start/end.

If the user asks "how much time on X today" → use `total_minutes`.

# Style

Keep replies short and direct. No preamble like "Sure!" or "Of course!".
When recommending a schedule, list times as a compact bulleted plan, then ask
if you should add it (don't add unless the user confirms or explicitly asks).
For destructive actions on tasks/projects with sessions, confirm first."""


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _opt_parse_dt(s: Any) -> datetime | None:
    return _parse_dt(s) if isinstance(s, str) and s else None


def _opt_parse_date(s: Any) -> date | None:
    return _parse_date(s) if isinstance(s, str) and s else None


def _tags_arg(args: dict) -> list[str]:
    raw = args.get("tags")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def build_mcp_server(store: EventStore):  # noqa: C901 - many small tools
    # ---- events ------------------------------------------------------

    @tool(
        "list_events",
        "List calendar events in a date/time range. ISO 8601 local datetimes.",
        {"start": str, "end": str},
    )
    async def list_events(args):
        events = store.list_range(_parse_dt(args["start"]), _parse_dt(args["end"]))
        return _ok(json.dumps([e.to_dict() for e in events], indent=2))

    @tool(
        "find_conflicts",
        "Return normal-priority timed events overlapping [start, end). "
        "Call before adding a normal-priority timed event.",
        {"start": str, "end": str},
    )
    async def find_conflicts(args):
        c = store.find_conflicts(_parse_dt(args["start"]), _parse_dt(args["end"]))
        return _ok(json.dumps([e.to_dict() for e in c], indent=2))

    @tool(
        "add_event",
        "Add a calendar event. start/end are ISO datetimes. Optional: tags "
        "(list), all_day (bool), priority ('normal'|'low'), notes. "
        "For all-day, start=00:00, end=00:00 next day.",
        {"title": str, "start": str, "end": str},
    )
    async def add_event(args):
        ev = store.add(
            title=args["title"],
            start=_parse_dt(args["start"]),
            end=_parse_dt(args["end"]),
            notes=args.get("notes", "") or "",
            tags=_tags_arg(args),
            all_day=bool(args.get("all_day", False)),
            priority=str(args.get("priority", "normal")),
        )
        return _ok(f"Added event #{ev.id}: {json.dumps(ev.to_dict())}")

    @tool(
        "add_recurring",
        "Add a recurring event series. recurrence: daily|weekly|monthly|yearly. "
        "until is YYYY-MM-DD.",
        {"title": str, "start": str, "end": str, "recurrence": str, "until": str},
    )
    async def add_recurring(args):
        sid, instances = store.add_recurring(
            title=args["title"],
            start=_parse_dt(args["start"]),
            end=_parse_dt(args["end"]),
            recurrence=args["recurrence"],
            until=_parse_date(args["until"]),
            notes=args.get("notes", "") or "",
            tags=_tags_arg(args),
            all_day=bool(args.get("all_day", False)),
            priority=str(args.get("priority", "normal")),
        )
        first = instances[0].to_dict() if instances else None
        return _ok(
            f"Added series {sid} — {len(instances)} instances of '{args['title']}' "
            f"({args['recurrence']}, first: {json.dumps(first)})"
        )

    @tool(
        "update_event",
        "Update an event by id. Include only fields to change.",
        {"id": int},
    )
    async def update_event(args):
        ev = store.update(
            event_id=int(args["id"]),
            title=args.get("title"),
            start=_opt_parse_dt(args.get("start")),
            end=_opt_parse_dt(args.get("end")),
            notes=args.get("notes"),
            tags=_tags_arg(args) if "tags" in args else None,
            all_day=bool(args["all_day"]) if "all_day" in args else None,
            priority=str(args["priority"]) if "priority" in args else None,
        )
        if ev is None:
            return _err(f"No event #{args['id']}")
        return _ok(f"Updated event #{ev.id}: {json.dumps(ev.to_dict())}")

    @tool("delete_event", "Delete an event by id.", {"id": int})
    async def delete_event(args):
        if not store.delete(int(args["id"])):
            return _err(f"No event #{args['id']}")
        return _ok(f"Deleted event #{args['id']}")

    @tool("delete_series", "Delete an entire recurring series by series_id.", {"series_id": str})
    async def delete_series(args):
        n = store.delete_series(args["series_id"])
        if n == 0:
            return _err(f"No series {args['series_id']}")
        return _ok(f"Deleted {n} instances of series {args['series_id']}")

    # ---- projects ----------------------------------------------------

    @tool(
        "list_projects",
        "List projects. By default excludes archived; pass include_archived=true "
        "to include them.",
        {},
    )
    async def list_projects(args):
        include = bool(args.get("include_archived", False))
        ps = store.list_projects(include_archived=include)
        return _ok(json.dumps([p.to_dict() for p in ps], indent=2))

    @tool(
        "add_project",
        "Create a new project. Returns its id. If a project with this name "
        "already exists, returns that one (idempotent).",
        {"name": str},
    )
    async def add_project(args):
        existing = store.find_project_by_name(args["name"])
        if existing:
            return _ok(f"Project '{existing.name}' already exists (#{existing.id})")
        p = store.add_project(args["name"])
        return _ok(f"Added project #{p.id} '{p.name}'")

    @tool(
        "update_project",
        "Rename or archive a project by id.",
        {"id": int},
    )
    async def update_project(args):
        p = store.update_project(
            int(args["id"]),
            name=args.get("name"),
            archived=bool(args["archived"]) if "archived" in args else None,
        )
        if p is None:
            return _err(f"No project #{args['id']}")
        return _ok(f"Updated project #{p.id}: {json.dumps(p.to_dict())}")

    @tool(
        "delete_project",
        "Delete a project AND all its tasks and sessions. Destructive.",
        {"id": int},
    )
    async def delete_project(args):
        if not store.delete_project(int(args["id"])):
            return _err(f"No project #{args['id']}")
        return _ok(f"Deleted project #{args['id']} (and its tasks/sessions)")

    # ---- tasks -------------------------------------------------------

    @tool(
        "list_tasks",
        "List tasks. Optional filters: project_id (int), status ('open'|'done'), "
        "scheduled_on (YYYY-MM-DD), include_done (bool, default true).",
        {},
    )
    async def list_tasks(args):
        ts = store.list_tasks(
            project_id=int(args["project_id"]) if "project_id" in args else None,
            status=args.get("status"),
            scheduled_on=_opt_parse_date(args.get("scheduled_on")),
            include_done=bool(args.get("include_done", True)),
        )
        return _ok(json.dumps([t.to_dict() for t in ts], indent=2))

    @tool(
        "add_task",
        "Add a task under a project. project_id is required. Optional: notes, "
        "scheduled_for (ISO datetime), estimate_minutes (int).",
        {"project_id": int, "title": str},
    )
    async def add_task(args):
        try:
            t = store.add_task(
                project_id=int(args["project_id"]),
                title=args["title"],
                notes=args.get("notes", "") or "",
                scheduled_for=_opt_parse_dt(args.get("scheduled_for")),
                estimate_minutes=int(args["estimate_minutes"])
                if args.get("estimate_minutes") is not None
                else None,
            )
        except Exception as e:  # noqa: BLE001
            return _err(str(e))
        return _ok(f"Added task #{t.id}: {json.dumps(t.to_dict())}")

    @tool(
        "update_task",
        "Update task fields by id. To clear scheduled_for, pass clear_scheduled=true.",
        {"id": int},
    )
    async def update_task(args):
        t = store.update_task(
            task_id=int(args["id"]),
            title=args.get("title"),
            notes=args.get("notes"),
            status=args.get("status"),
            project_id=int(args["project_id"]) if "project_id" in args else None,
            scheduled_for=_opt_parse_dt(args.get("scheduled_for")),
            clear_scheduled=bool(args.get("clear_scheduled", False)),
            estimate_minutes=int(args["estimate_minutes"])
            if args.get("estimate_minutes") is not None
            else None,
        )
        if t is None:
            return _err(f"No task #{args['id']}")
        return _ok(f"Updated task #{t.id}: {json.dumps(t.to_dict())}")

    @tool("complete_task", "Mark a task as done. Sets completed_at.", {"id": int})
    async def complete_task(args):
        t = store.complete_task(int(args["id"]))
        if t is None:
            return _err(f"No task #{args['id']}")
        return _ok(f"Completed task #{t.id} '{t.title}'")

    @tool("delete_task", "Delete a task AND its sessions. Destructive.", {"id": int})
    async def delete_task(args):
        if not store.delete_task(int(args["id"])):
            return _err(f"No task #{args['id']}")
        return _ok(f"Deleted task #{args['id']}")

    # ---- sessions ----------------------------------------------------

    @tool(
        "start_session",
        "Start a time-tracking session on a task. Optional start (ISO datetime, "
        "defaults to now).",
        {"task_id": int},
    )
    async def start_session(args):
        try:
            sess = store.start_session(
                int(args["task_id"]),
                start=_opt_parse_dt(args.get("start")),
            )
        except ValueError as e:
            return _err(str(e))
        return _ok(f"Started session #{sess.id}: {json.dumps(sess.to_dict())}")

    @tool(
        "stop_session",
        "Stop an ongoing session by id. Optional end (ISO datetime, defaults to now).",
        {"id": int},
    )
    async def stop_session(args):
        sess = store.stop_session(
            int(args["id"]), end=_opt_parse_dt(args.get("end"))
        )
        if sess is None:
            return _err(f"No session #{args['id']}")
        return _ok(f"Stopped session #{sess.id}: {json.dumps(sess.to_dict())}")

    @tool(
        "stop_all_ongoing",
        "Stop every currently-ongoing session at end (default now).",
        {},
    )
    async def stop_all_ongoing(args):
        n = store.stop_all_ongoing(end=_opt_parse_dt(args.get("end")))
        return _ok(f"Stopped {n} ongoing session(s)")

    @tool(
        "add_session",
        "Backfill a completed session on a task. Both start and end required.",
        {"task_id": int, "start": str, "end": str},
    )
    async def add_session(args):
        try:
            sess = store.add_session(
                int(args["task_id"]),
                _parse_dt(args["start"]),
                _parse_dt(args["end"]),
                notes=args.get("notes", "") or "",
            )
        except ValueError as e:
            return _err(str(e))
        return _ok(f"Added session #{sess.id}: {json.dumps(sess.to_dict())}")

    @tool(
        "update_session",
        "Patch a session by id. To turn a closed session back into an ongoing "
        "one, pass clear_end=true.",
        {"id": int},
    )
    async def update_session(args):
        sess = store.update_session(
            int(args["id"]),
            start=_opt_parse_dt(args.get("start")),
            end=_opt_parse_dt(args.get("end")),
            clear_end=bool(args.get("clear_end", False)),
            notes=args.get("notes"),
        )
        if sess is None:
            return _err(f"No session #{args['id']}")
        return _ok(f"Updated session #{sess.id}: {json.dumps(sess.to_dict())}")

    @tool("delete_session", "Delete a session by id.", {"id": int})
    async def delete_session(args):
        if not store.delete_session(int(args["id"])):
            return _err(f"No session #{args['id']}")
        return _ok(f"Deleted session #{args['id']}")

    @tool(
        "list_sessions",
        "List sessions. Optional filters: task_id, start/end (window), ongoing_only.",
        {},
    )
    async def list_sessions(args):
        sessions = store.list_sessions(
            task_id=int(args["task_id"]) if "task_id" in args else None,
            start=_opt_parse_dt(args.get("start")),
            end=_opt_parse_dt(args.get("end")),
            ongoing_only=bool(args.get("ongoing_only", False)),
        )
        return _ok(json.dumps([s.to_dict() for s in sessions], indent=2))

    @tool(
        "total_minutes",
        "Sum of tracked minutes. Optional filters: task_id, project_id, on_date "
        "(YYYY-MM-DD). Ongoing sessions count up to 'now'.",
        {},
    )
    async def total_minutes(args):
        n = store.total_minutes(
            task_id=int(args["task_id"]) if "task_id" in args else None,
            project_id=int(args["project_id"]) if "project_id" in args else None,
            on_date=_opt_parse_date(args.get("on_date")),
        )
        return _ok(f"{n} minutes")

    return create_sdk_mcp_server(
        name="cal",
        version="0.3.0",
        tools=[
            # events
            list_events, find_conflicts, add_event, add_recurring,
            update_event, delete_event, delete_series,
            # projects
            list_projects, add_project, update_project, delete_project,
            # tasks
            list_tasks, add_task, update_task, complete_task, delete_task,
            # sessions
            start_session, stop_session, stop_all_ongoing, add_session,
            update_session, delete_session, list_sessions, total_minutes,
        ],
    )


StreamCallback = Callable[[str], Awaitable[None]]
ToolCallback = Callable[[str, dict], Awaitable[None]]
ResultCallback = Callable[[ResultMessage], Awaitable[None]]


class PlannerAgent:
    """Wraps a long-lived ClaudeSDKClient so chat history persists across turns."""

    def __init__(self, store: EventStore):
        self._store = store
        self._server = build_mcp_server(store)
        allowed = [f"mcp__cal__{name}" for name in [
            "list_events", "find_conflicts", "add_event", "add_recurring",
            "update_event", "delete_event", "delete_series",
            "list_projects", "add_project", "update_project", "delete_project",
            "list_tasks", "add_task", "update_task", "complete_task", "delete_task",
            "start_session", "stop_session", "stop_all_ongoing", "add_session",
            "update_session", "delete_session", "list_sessions", "total_minutes",
        ]]
        self._options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"cal": self._server},
            allowed_tools=allowed,
        )
        self._client: ClaudeSDKClient | None = None

    async def start(self) -> None:
        self._client = ClaudeSDKClient(options=self._options)
        await self._client.connect()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def send(
        self,
        user_text: str,
        *,
        view: str,
        anchor: date,
        on_text: StreamCallback,
        on_tool: ToolCallback,
        on_result: ResultCallback | None = None,
    ) -> None:
        if self._client is None:
            raise RuntimeError("Agent not started")

        now = datetime.now().replace(microsecond=0)
        wk_start = week_start(anchor)
        ctx = (
            f"[context] now={now.isoformat(timespec='minutes')} "
            f"view={view} anchor_date={anchor.isoformat()} "
            f"week_start={wk_start.isoformat()} "
            f"week_end={(wk_start + timedelta(days=6)).isoformat()}"
        )
        await self._client.query(f"{ctx}\n\n{user_text}")

        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        await on_text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        await on_tool(block.name, block.input)
            elif isinstance(message, ResultMessage):
                if on_result is not None:
                    await on_result(message)
                return
