from __future__ import annotations

from datetime import date, datetime, timedelta

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from planner.db import Event, Project, Session, Task, tag_color, week_start

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HOURS = list(range(7, 23))  # 07:00..22:00


def _tag_chips(tags: list[str]) -> Text:
    out = Text()
    for i, t in enumerate(tags):
        if i:
            out.append(" ")
        out.append(f"#{t}", style=tag_color(t))
    return out


def _event_style(ev: Event) -> str:
    if ev.priority == "low":
        return "dim"
    return ev.primary_color


def _split_day_events(events: list[Event]) -> tuple[list[Event], list[Event]]:
    """Returns (timed, all_day)."""
    timed = [e for e in events if not e.all_day]
    allday = [e for e in events if e.all_day]
    return timed, allday


def render_week(
    anchor: date,
    events: list[Event],
    worked_by_day: dict[date, list[tuple[Task, int]]] | None = None,
) -> Panel:
    monday = week_start(anchor)
    days = [monday + timedelta(days=i) for i in range(7)]
    worked_by_day = worked_by_day or {}

    by_day: dict[date, list[Event]] = {d: [] for d in days}
    for ev in events:
        d = ev.start.date()
        if d in by_day:
            by_day[d].append(ev)

    today = date.today()
    table = Table(expand=True, show_lines=True, pad_edge=False)
    for d, name in zip(days, DAY_NAMES):
        header = Text()
        header.append(f"{name} ", style="bold")
        header.append(d.strftime("%m/%d"), style="dim")
        if d == today:
            header.stylize("bold cyan")
        if d == anchor:
            # Distinct from "today" so the user can tell which day Tab will
            # move from; combines with "today" style when they coincide.
            header.stylize("reverse")
        table.add_column(header, ratio=1, overflow="fold")

    cells: list[Text] = []
    for d in days:
        day_events = by_day[d]
        worked = worked_by_day.get(d, [])
        if not day_events and not worked:
            cells.append(Text("—", style="dim"))
            continue

        timed, allday = _split_day_events(day_events)
        cell = Text()

        for ev in allday:
            if cell.plain:
                cell.append("\n")
            cell.append("· ", style="dim")
            cell.append(ev.title, style="dim italic")

        for ev in timed:
            if cell.plain:
                cell.append("\n")
            cell.append(ev.start.strftime("%H:%M "), style="cyan")
            cell.append(ev.title, style=_event_style(ev))
            cell.append(f" #{ev.id}", style="dim")
            if ev.tags:
                cell.append("  ")
                cell.append_text(_tag_chips(ev.tags))

        if worked:
            if cell.plain:
                cell.append("\n")
            cell.append("\n─ tracked ─", style="dim italic")
            for task, mins in worked:
                cell.append("\n• ", style="dim")
                cell.append(task.title)
                cell.append(f"  {_fmt_minutes(mins)}", style="cyan")

        cells.append(cell)
    table.add_row(*cells)

    sunday = monday + timedelta(days=6)
    title = f"Week of {monday.strftime('%b %d')} – {sunday.strftime('%b %d, %Y')}"
    return Panel(table, title=title, border_style="cyan")


def render_day(anchor: date, events: list[Event]) -> Panel:
    timed, allday = _split_day_events(events)

    body: list = []

    if allday:
        band = Text()
        band.append("All day:  ", style="bold dim")
        for i, ev in enumerate(allday):
            if i:
                band.append("  ·  ", style="dim")
            band.append(ev.title, style=_event_style(ev))
            if ev.tags:
                band.append(" ")
                band.append_text(_tag_chips(ev.tags))
            band.append(f" #{ev.id}", style="dim")
        body.append(band)
        body.append(Text(""))  # spacer

    table = Table(expand=True, show_header=False, pad_edge=False)
    table.add_column("time", width=7, style="dim")
    table.add_column("event", ratio=1, overflow="fold")

    by_hour: dict[int, list[Event]] = {h: [] for h in HOURS}
    outside: list[Event] = []
    for ev in timed:
        h = ev.start.hour
        if h in by_hour:
            by_hour[h].append(ev)
        else:
            outside.append(ev)

    for h in HOURS:
        label = f"{h:02d}:00"
        if not by_hour[h]:
            table.add_row(label, Text("·", style="dim"))
            continue
        cell = Text()
        for i, ev in enumerate(by_hour[h]):
            if i:
                cell.append("\n")
            span = ev.start.strftime("%H:%M") + "–" + ev.end.strftime("%H:%M")
            cell.append(span + "  ", style="cyan")
            cell.append(ev.title, style="bold " + _event_style(ev))
            cell.append(f"  #{ev.id}", style="dim")
            if ev.tags:
                cell.append("  ")
                cell.append_text(_tag_chips(ev.tags))
            if ev.notes:
                cell.append(f"\n        {ev.notes}", style="italic dim")
        table.add_row(label, cell)

    body.append(table)

    if outside:
        extra = Text("Outside grid:\n", style="bold")
        for ev in outside:
            extra.append(f"  #{ev.id} ")
            extra.append(ev.start.strftime("%H:%M  "), style="cyan")
            extra.append(ev.title + "\n", style=_event_style(ev))
        body.append(extra)

    today_marker = "  (today)" if anchor == date.today() else ""
    title = anchor.strftime("%A %B %d, %Y") + today_marker
    return Panel(Group(*body), title=title, border_style="cyan")


# ---- agenda view ------------------------------------------------------


def _fmt_minutes(n: int) -> str:
    if n < 60:
        return f"{n}m"
    h, m = divmod(n, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def _project_chip(project: Project | None) -> Text:
    if project is None:
        return Text()
    return Text(project.name, style=tag_color(project.name))


def _session_intervals(sessions: list[Session]) -> Text:
    """Render today's sessions as '14:05-14:30, 15:00-now' style chips."""
    out = Text()
    for i, sess in enumerate(sorted(sessions, key=lambda s: s.start)):
        if i:
            out.append(", ", style="dim")
        out.append(sess.start.strftime("%H:%M"), style="cyan")
        out.append("–", style="dim")
        if sess.end is None:
            out.append("now", style="bold yellow")
        else:
            out.append(sess.end.strftime("%H:%M"), style="cyan")
    return out


def render_agenda(
    anchor: date,
    events: list[Event],
    tasks_for_day: list[Task],
    backlog_tasks: list[Task],
    ongoing_sessions: list[tuple[Session, Task, Project | None]],
    projects_by_id: dict[int, Project],
    sessions_by_task: dict[int, list[Session]] | None = None,
    *,
    now: datetime | None = None,
) -> Panel:
    """One panel showing today: ongoing → scheduled (events+tasks) → backlog → all-day."""
    now = now or datetime.now()
    sessions_by_task = sessions_by_task or {}
    body: list = []

    # NOW: ongoing sessions ----------------------------------------------
    if ongoing_sessions:
        for sess, task, project in ongoing_sessions:
            line = Text()
            line.append("▶ NOW   ", style="bold yellow")
            line.append(task.title, style="bold")
            line.append("   since ", style="dim")
            line.append(sess.start.strftime("%H:%M"), style="cyan")
            line.append(
                f" · {_fmt_minutes(sess.duration_minutes(now=now))}", style="cyan"
            )
            if project is not None:
                line.append("   ")
                line.append_text(_project_chip(project))
            line.append(f"   task #{task.id} · sess #{sess.id}", style="dim")
            body.append(line)
        body.append(Text(""))

    # SCHEDULED: events (non-all-day) + tasks with scheduled_for ----------
    timed_events = [e for e in events if not e.all_day]
    scheduled_items: list[tuple[datetime, str, object]] = []
    for ev in timed_events:
        scheduled_items.append((ev.start, "ev", ev))
    for t in tasks_for_day:
        if t.scheduled_for is not None:
            scheduled_items.append((t.scheduled_for, "task", t))
    scheduled_items.sort(key=lambda x: x[0])

    sched_tbl = Table(expand=True, show_header=False, padding=(0, 1), pad_edge=False)
    sched_tbl.add_column("time", width=7, style="cyan", no_wrap=True)
    sched_tbl.add_column("kind", width=5, style="dim", no_wrap=True)
    sched_tbl.add_column("body", ratio=1, overflow="fold")

    if scheduled_items:
        for t, kind, obj in scheduled_items:
            time_str = t.strftime("%H:%M")
            cell = Text()
            if kind == "ev":
                ev = obj  # type: ignore[assignment]
                cell.append(ev.title, style=_event_style(ev))
                cell.append(f"  #{ev.id}", style="dim")
                if ev.tags:
                    cell.append("  ")
                    cell.append_text(_tag_chips(ev.tags))
            else:
                task = obj  # type: ignore[assignment]
                cell.append(task.title, style="bold")
                if task.estimate_minutes:
                    cell.append(f"  ({_fmt_minutes(task.estimate_minutes)})", style="dim")
                if task.status == "done":
                    cell.append("  ✓", style="green")
                task_sessions = sessions_by_task.get(task.id, [])
                if task_sessions:
                    cell.append("  · ", style="dim")
                    cell.append_text(_session_intervals(task_sessions))
                cell.append(f"  task #{task.id}", style="dim")
                proj = projects_by_id.get(task.project_id)
                if proj is not None:
                    cell.append("  ")
                    cell.append_text(_project_chip(proj))
            sched_tbl.add_row(time_str, kind, cell)
    else:
        sched_tbl.add_row("", "", Text("— nothing scheduled —", style="dim"))

    body.append(Text("Schedule", style="bold underline"))
    body.append(sched_tbl)

    # BACKLOG: open tasks with no scheduled_for --------------------------
    if backlog_tasks:
        body.append(Text(""))
        section_title = (
            "Tasks"
            if anchor == date.today()
            else "Worked this day"
        )
        body.append(Text(section_title, style="bold underline"))
        bl_tbl = Table(expand=True, show_header=False, padding=(0, 1), pad_edge=False)
        bl_tbl.add_column("body", ratio=1, overflow="fold")
        for task in backlog_tasks:
            cell = Text()
            cell.append("• ", style="dim")
            cell.append(task.title)
            if task.estimate_minutes:
                cell.append(f"  ({_fmt_minutes(task.estimate_minutes)})", style="dim")
            task_sessions = sessions_by_task.get(task.id, [])
            if task_sessions:
                cell.append("  · ", style="dim")
                cell.append_text(_session_intervals(task_sessions))
            cell.append(f"   task #{task.id}", style="dim")
            proj = projects_by_id.get(task.project_id)
            if proj is not None:
                cell.append("  ")
                cell.append_text(_project_chip(proj))
            bl_tbl.add_row(cell)
        body.append(bl_tbl)

    # ALL-DAY events -----------------------------------------------------
    allday = [e for e in events if e.all_day]
    if allday:
        body.append(Text(""))
        body.append(Text("All-day", style="bold underline"))
        for ev in allday:
            line = Text()
            line.append("• ", style="dim")
            line.append(ev.title, style=_event_style(ev))
            line.append(f"   #{ev.id}", style="dim")
            if ev.tags:
                line.append("  ")
                line.append_text(_tag_chips(ev.tags))
            body.append(line)

    today_marker = "  (today)" if anchor == date.today() else ""
    title = "Agenda · " + anchor.strftime("%A %B %d, %Y") + today_marker
    return Panel(Group(*body), title=title, border_style="green")
