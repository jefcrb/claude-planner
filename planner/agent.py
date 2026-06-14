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

You have tools to read and modify the user's calendar in a local SQLite database.
Always confirm what you changed in plain language after using a tool.

Datetime format: ISO 8601 local time, e.g. "2026-06-14T09:30".
When the user uses relative terms ("tomorrow", "next Tuesday", "in 2 hours"),
resolve them against the provided "now" context and pass concrete ISO strings to tools.

# Event metadata

Every event has these fields you should set thoughtfully:

- **tags**: 1-3 short lowercase tags inferred from the title and notes. Stable
  across similar events (so "team standup" and "standup" both get
  ["work", "meeting"]). Common families: work, meeting, deep, personal, health,
  social, travel, family, holiday, birthday, learning, chore.
- **all_day**: true for events with no specific time (birthdays, holidays,
  travel days, anniversaries). false for timed events.
- **priority**: "normal" for things the user actively plans around;
  "low" for ambient context that should never trigger a conflict warning
  (birthdays, public holidays, "rainy day" notes). All_day events are
  almost always priority="low".

# Conflicts

Before adding a NORMAL-priority timed event, call `find_conflicts` for that
time window. If anything overlaps, tell the user what conflicts and ask whether
to book over it, move the new event, or cancel. Never silently overwrite.

# Recurring events

Use `add_recurring` for anything that repeats:
- Birthdays/anniversaries: recurrence="yearly", all_day=true, priority="low".
- Standups/weekly syncs: recurrence="weekly", normal priority.
- Daily habits: recurrence="daily".

Pick a sensible "until" date if the user doesn't give one:
- yearly: 5 years out
- weekly/daily: end of current year (or until next natural break)

Use `delete_series` to remove a whole recurring series at once.

# Style

Keep replies short and direct. No preamble like "Sure!" or "Of course!".
When recommending a schedule, list times as a compact bulleted plan, then ask
if you should add it (don't add unless the user confirms or explicitly asks)."""


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


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


def build_mcp_server(store: EventStore):
    @tool(
        "list_events",
        "List events in a date/time range. Use ISO 8601 local datetimes.",
        {"start": str, "end": str},
    )
    async def list_events(args: dict[str, Any]) -> dict[str, Any]:
        events = store.list_range(_parse_dt(args["start"]), _parse_dt(args["end"]))
        return _ok(json.dumps([e.to_dict() for e in events], indent=2))

    @tool(
        "find_conflicts",
        "Return normal-priority timed events that overlap [start, end). "
        "Call this before adding a normal-priority timed event. "
        "All-day and low-priority events are intentionally excluded.",
        {"start": str, "end": str},
    )
    async def find_conflicts(args: dict[str, Any]) -> dict[str, Any]:
        conflicts = store.find_conflicts(
            _parse_dt(args["start"]), _parse_dt(args["end"])
        )
        return _ok(json.dumps([e.to_dict() for e in conflicts], indent=2))

    @tool(
        "add_event",
        "Add a new event. start and end are ISO 8601 local datetimes. "
        "tags (list of short lowercase strings), all_day (bool), "
        "priority ('normal' or 'low'), notes are optional. "
        "For all-day events, pass start at 00:00 and end at 00:00 the next day.",
        {"title": str, "start": str, "end": str},
    )
    async def add_event(args: dict[str, Any]) -> dict[str, Any]:
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
        "Add a recurring event as a series of concrete instances sharing a "
        "series_id. recurrence is one of: daily, weekly, monthly, yearly. "
        "until is an ISO date (YYYY-MM-DD) — pick a sensible cap if the user "
        "doesn't supply one (yearly: 5 years out; weekly/daily: end of year).",
        {
            "title": str,
            "start": str,
            "end": str,
            "recurrence": str,
            "until": str,
        },
    )
    async def add_recurring(args: dict[str, Any]) -> dict[str, Any]:
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
        "Update an existing event by id. Only include fields you want to change. "
        "start/end are ISO 8601 local datetimes.",
        {"id": int},
    )
    async def update_event(args: dict[str, Any]) -> dict[str, Any]:
        tags = _tags_arg(args) if "tags" in args else None
        ev = store.update(
            event_id=int(args["id"]),
            title=args.get("title"),
            start=_parse_dt(args["start"]) if args.get("start") else None,
            end=_parse_dt(args["end"]) if args.get("end") else None,
            notes=args.get("notes"),
            tags=tags,
            all_day=bool(args["all_day"]) if "all_day" in args else None,
            priority=str(args["priority"]) if "priority" in args else None,
        )
        if ev is None:
            return _err(f"No event with id {args['id']}")
        return _ok(f"Updated event #{ev.id}: {json.dumps(ev.to_dict())}")

    @tool(
        "delete_event",
        "Delete an event by id.",
        {"id": int},
    )
    async def delete_event(args: dict[str, Any]) -> dict[str, Any]:
        ok = store.delete(int(args["id"]))
        if not ok:
            return _err(f"No event with id {args['id']}")
        return _ok(f"Deleted event #{args['id']}")

    @tool(
        "delete_series",
        "Delete every instance of a recurring series by series_id.",
        {"series_id": str},
    )
    async def delete_series(args: dict[str, Any]) -> dict[str, Any]:
        n = store.delete_series(args["series_id"])
        if n == 0:
            return _err(f"No series with id {args['series_id']}")
        return _ok(f"Deleted {n} instances of series {args['series_id']}")

    return create_sdk_mcp_server(
        name="cal",
        version="0.2.0",
        tools=[
            list_events,
            find_conflicts,
            add_event,
            add_recurring,
            update_event,
            delete_event,
            delete_series,
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
        self._options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"cal": self._server},
            allowed_tools=[
                "mcp__cal__list_events",
                "mcp__cal__find_conflicts",
                "mcp__cal__add_event",
                "mcp__cal__add_recurring",
                "mcp__cal__update_event",
                "mcp__cal__delete_event",
                "mcp__cal__delete_series",
            ],
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
