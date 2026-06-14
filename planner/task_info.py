from __future__ import annotations

from datetime import date, datetime

from rich.console import Group
from rich.text import Text
from textual.widgets import Static

from planner.db import EventStore, tag_color


def _fmt_minutes(n: int) -> str:
    if n < 60:
        return f"{n}m"
    h, m = divmod(n, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


class TaskInfoPanel(Static):
    """Bottom-right side panel with ongoing sessions + today stats."""

    def __init__(self, store: EventStore) -> None:
        super().__init__("", id="task-info", markup=False)
        self.store = store
        self._timer = None

    def on_mount(self) -> None:
        self.refresh_panel()
        # Tick every 30s so elapsed minutes stay close to current.
        self._timer = self.set_interval(30.0, self.refresh_panel)

    def refresh_panel(self) -> None:
        now = datetime.now()
        ongoing = self.store.list_sessions(ongoing_only=True)
        projects = {p.id: p for p in self.store.list_projects(include_archived=True)}

        body: list = []

        header = Text()
        header.append("Tasks", style="bold")
        if ongoing:
            header.append(f"   ▶ {len(ongoing)}", style="bold yellow")
        body.append(header)
        body.append(Text(""))

        if ongoing:
            for sess in ongoing:
                task = self.store.get_task(sess.task_id)
                if task is None:
                    continue
                line1 = Text(overflow="ellipsis", no_wrap=True)
                line1.append("▶ ", style="bold yellow")
                line1.append(task.title, style="bold")
                body.append(line1)

                line2 = Text()
                line2.append("  since ", style="dim")
                line2.append(sess.start.strftime("%H:%M"), style="cyan")
                line2.append(" · ", style="dim")
                line2.append(
                    _fmt_minutes(sess.duration_minutes(now=now)),
                    style="bold cyan",
                )
                body.append(line2)

                proj = projects.get(task.project_id)
                if proj is not None:
                    line3 = Text()
                    line3.append("  ", style="dim")
                    line3.append(proj.name, style=tag_color(proj.name))
                    body.append(line3)

                body.append(Text(""))
        else:
            body.append(Text("Nothing running", style="dim italic"))
            body.append(Text(""))

        # Today stats -----------------------------------------------------
        today = date.today()
        today_mins = self.store.total_minutes(on_date=today)
        open_tasks = [
            t for t in self.store.list_tasks(include_done=False)
        ]
        scheduled_today = [
            t for t in self.store.list_tasks(scheduled_on=today)
            if t.status != "done"
        ]

        body.append(Text("─" * 28, style="dim"))
        stats = Text()
        stats.append("Today  ", style="dim")
        stats.append(_fmt_minutes(today_mins), style="bold cyan")
        body.append(stats)

        open_line = Text()
        open_line.append("Open   ", style="dim")
        open_line.append(str(len(open_tasks)), style="bold")
        open_line.append(" tasks", style="dim")
        body.append(open_line)

        sched_line = Text()
        sched_line.append("Sched  ", style="dim")
        sched_line.append(str(len(scheduled_today)), style="bold")
        sched_line.append(" today", style="dim")
        body.append(sched_line)

        self.update(Group(*body))
