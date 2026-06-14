from __future__ import annotations

from datetime import date, datetime, timedelta

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from planner.db import Event, tag_color, week_start

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


def render_week(anchor: date, events: list[Event]) -> Panel:
    monday = week_start(anchor)
    days = [monday + timedelta(days=i) for i in range(7)]

    by_day: dict[date, list[Event]] = {d: [] for d in days}
    for ev in events:
        d = ev.start.date()
        if d in by_day:
            by_day[d].append(ev)

    table = Table(expand=True, show_lines=True, pad_edge=False)
    for d, name in zip(days, DAY_NAMES):
        header = Text()
        header.append(f"{name} ", style="bold")
        header.append(d.strftime("%m/%d"), style="dim")
        if d == date.today():
            header.stylize("bold cyan")
        table.add_column(header, ratio=1, overflow="fold")

    cells: list[Text] = []
    for d in days:
        day_events = by_day[d]
        if not day_events:
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
