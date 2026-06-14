from __future__ import annotations

import json
from datetime import date, timedelta

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from planner.agent import PlannerAgent
from planner.db import EventStore, week_start
from planner.history import HistoryScreen
from planner.status import StatusBar, format_usage
from planner.task_info import TaskInfoPanel
from planner.views import render_agenda, render_day, render_week

HELP_TEXT = (
    "[bold]Hotkeys[/bold]  "
    "[cyan]w[/]=week  [cyan]d[/]=day  [cyan]a[/]=agenda  "
    "[cyan]←[/] prev  [cyan]→[/] next  "
    "[cyan]Tab[/]/[cyan]Shift+Tab[/] day  "
    "[cyan]t[/]=today  [cyan]i[/]=focus chat  "
    "[cyan]Esc[/]=unfocus chat  [cyan]p[/]=projects  [cyan]h[/]=history  "
    "[cyan]?[/]=help  [cyan]q[/]=quit"
)


class ChatInput(Input):
    """Input that releases focus back to the app on ESC."""

    BINDINGS = [Binding("escape", "unfocus_chat", "Exit chat", show=True)]

    def action_unfocus_chat(self) -> None:
        self.app.set_focus(None)


class CalendarView(Static):
    """Renders either the week grid or the day grid."""

    def __init__(self, store: EventStore):
        super().__init__(id="calendar")
        self.store = store
        self.mode: str = "week"
        self.anchor: date = date.today()

    def on_mount(self) -> None:
        self.refresh_view()

    def refresh_view(self) -> None:
        if self.mode == "week":
            from datetime import datetime, time, timedelta
            monday = week_start(self.anchor)
            events_ = self.store.list_week(monday)
            wk_start = datetime.combine(monday, time.min)
            wk_end = wk_start + timedelta(days=7)
            sessions_week = self.store.list_sessions(start=wk_start, end=wk_end)
            now = datetime.now()
            # For each session, split into per-day chunks (clamped to the
            # week window) and accumulate minutes per (date, task_id).
            per_day_task: dict = {}
            for sess in sessions_week:
                s_start = max(sess.start, wk_start)
                s_end = min(sess.end or now, wk_end)
                cur = s_start
                while cur < s_end:
                    d = cur.date()
                    day_end = min(
                        s_end,
                        datetime.combine(d + timedelta(days=1), time.min),
                    )
                    mins = max(0, int((day_end - cur).total_seconds() // 60))
                    if mins:
                        per_day_task.setdefault(d, {}).setdefault(sess.task_id, 0)
                        per_day_task[d][sess.task_id] += mins
                    cur = day_end
            worked_by_day = {}
            for d, by_task in per_day_task.items():
                items = []
                for tid, mins in by_task.items():
                    t = self.store.get_task(tid)
                    if t is not None:
                        items.append((t, mins))
                items.sort(key=lambda x: -x[1])
                worked_by_day[d] = items
            self.update(render_week(self.anchor, events_, worked_by_day))
        elif self.mode == "day":
            events_ = self.store.list_day(self.anchor)
            self.update(render_day(self.anchor, events_))
        else:  # agenda
            from datetime import datetime, time, timedelta

            events_ = self.store.list_day(self.anchor)
            tasks_day = self.store.list_tasks(scheduled_on=self.anchor)
            projects = {p.id: p for p in self.store.list_projects(include_archived=True)}

            day_start = datetime.combine(self.anchor, time.min)
            day_end = day_start + timedelta(days=1)
            sessions_today = self.store.list_sessions(start=day_start, end=day_end)
            sessions_by_task: dict[int, list] = {}
            for sess in sessions_today:
                sessions_by_task.setdefault(sess.task_id, []).append(sess)

            # Backlog rules:
            #  - On today: show the full open backlog (tasks with no
            #    scheduled_for) — it's your "what to pick up next" list.
            #  - On other days: show ONLY unscheduled tasks that were
            #    actually worked on that day, so their session times are
            #    visible without polluting every day with unrelated tasks.
            if self.anchor == date.today():
                backlog = [
                    t for t in self.store.list_tasks(include_done=False)
                    if t.scheduled_for is None
                ]
                shown_ids = {t.id for t in tasks_day} | {t.id for t in backlog}
                for tid in sessions_by_task:
                    if tid in shown_ids:
                        continue
                    t = self.store.get_task(tid)
                    if t is not None and t.scheduled_for is None:
                        backlog.append(t)
            else:
                backlog = []
                scheduled_ids = {t.id for t in tasks_day}
                for tid in sessions_by_task:
                    if tid in scheduled_ids:
                        continue
                    t = self.store.get_task(tid)
                    if t is not None and t.scheduled_for is None:
                        backlog.append(t)

            ongoing_rows = []
            for sess in self.store.list_sessions(ongoing_only=True):
                task = self.store.get_task(sess.task_id)
                if task is not None:
                    proj = projects.get(task.project_id)
                    ongoing_rows.append((sess, task, proj))

            self.update(
                render_agenda(
                    self.anchor, events_, tasks_day, backlog,
                    ongoing_rows, projects, sessions_by_task,
                )
            )

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.refresh_view()

    def step(self, direction: int) -> None:
        days = 7 if self.mode == "week" else 1
        self.anchor = self.anchor + timedelta(days=direction * days)
        self.refresh_view()

    def shift_anchor(self, direction: int) -> None:
        """Move anchor by 1 day, wrapping within the current week.

        Sunday + Tab → Monday of the same week.
        Monday + Shift+Tab → Sunday of the same week.
        Use ← / → to cross into adjacent weeks.
        """
        monday = week_start(self.anchor)
        new_weekday = (self.anchor.weekday() + direction) % 7
        self.anchor = monday + timedelta(days=new_weekday)
        self.refresh_view()

    def go_today(self) -> None:
        self.anchor = date.today()
        self.refresh_view()


class PlannerApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "Planner"

    BINDINGS = [
        Binding("w", "view_week", "Week"),
        Binding("d", "view_day", "Day"),
        Binding("a", "view_agenda", "Agenda"),
        Binding("left", "prev", "Prev"),
        Binding("right", "next", "Next"),
        Binding("tab", "next_day", "Next day", priority=True, show=False),
        Binding("shift+tab", "prev_day", "Prev day", priority=True, show=False),
        Binding("t", "today", "Today"),
        Binding("i", "focus_chat", "Chat"),
        Binding("p", "projects", "Projects"),
        Binding("h", "history", "History"),
        Binding("question_mark", "help", "Help"),
        Binding("ctrl+c,q", "quit", "Quit"),
    ]

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        # When the chat input is focused, hide every app-level binding from
        # the footer (and disable them) so the footer shows only the
        # ChatInput's Esc binding.
        if self._input is not None and self.focused is self._input:
            return None
        return True

    def __init__(self):
        super().__init__()
        self.store = EventStore()
        self.agent = PlannerAgent(self.store)
        self._calendar: CalendarView | None = None
        self._log: RichLog | None = None
        self._input: ChatInput | None = None
        self._status: StatusBar | None = None
        self._task_info: TaskInfoPanel | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._calendar = CalendarView(self.store)
        yield self._calendar
        with Horizontal(id="bottom-row"):
            with Vertical(id="chat"):
                self._log = RichLog(
                    id="chat-log", markup=True, wrap=True, highlight=False
                )
                self._log.can_focus = False
                yield self._log
                self._status = StatusBar()
                yield self._status
                self._input = ChatInput(
                    placeholder="Ask anything — 'add focus block tomorrow 9-11', 'plan my Tuesday'...",
                    id="chat-input",
                )
                yield self._input
            self._task_info = TaskInfoPanel(self.store)
            yield self._task_info
        yield Footer()

    async def on_mount(self) -> None:
        assert self._log is not None
        # Don't auto-focus the chat input — leave focus null so app bindings
        # (incl. arrows) own all keys until the user explicitly presses `i`.
        self.set_focus(None)
        self._log.write(HELP_TEXT)
        self._log.write(
            f"DB: [dim]{self.store.path}[/] · "
            f"history cap: [dim]{self.store.max_snapshots}[/]"
        )
        self._log.write("Starting Claude agent…")
        try:
            await self.agent.start()
            self._log.write("[green]Agent ready.[/] Type below and press Enter.")
        except Exception as e:  # noqa: BLE001
            self._log.write(f"[red]Agent failed to start:[/] {e}")
            self._log.write("Calendar still works; chat is disabled.")

    async def on_unmount(self) -> None:
        try:
            await self.agent.stop()
        except Exception:
            pass
        self.store.close()

    # ---- hotkey actions -------------------------------------------------

    def action_view_week(self) -> None:
        if self._calendar:
            self._calendar.set_mode("week")

    def action_view_day(self) -> None:
        if self._calendar:
            self._calendar.set_mode("day")

    def action_view_agenda(self) -> None:
        if self._calendar:
            self._calendar.set_mode("agenda")

    def action_projects(self) -> None:
        from planner.tasks_ui import ProjectsModal
        assert self._log is not None and self._calendar is not None

        def _refresh(_=None) -> None:
            assert self._calendar is not None
            self._calendar.refresh_view()
            if self._task_info is not None:
                self._task_info.refresh_panel()

        self.push_screen(ProjectsModal(self.store), _refresh)

    def action_prev(self) -> None:
        if self._calendar:
            self._calendar.step(-1)

    def action_next(self) -> None:
        if self._calendar:
            self._calendar.step(+1)

    def action_next_day(self) -> None:
        if self._calendar:
            self._calendar.shift_anchor(+1)

    def action_prev_day(self) -> None:
        if self._calendar:
            self._calendar.shift_anchor(-1)

    def action_today(self) -> None:
        if self._calendar:
            self._calendar.go_today()

    def action_focus_chat(self) -> None:
        if self._input:
            self._input.focus()

    def action_history(self) -> None:
        assert self._log is not None and self._calendar is not None
        snaps = self.store.list_snapshots()
        if not snaps:
            self._log.write("[yellow]No history yet — make a change first.[/]")
            return
        total = self.store.total_snapshots_size()

        def _on_pick(snapshot_id: int | None) -> None:
            if snapshot_id is None:
                return
            label = self.store.rollback(snapshot_id)
            assert self._log is not None and self._calendar is not None
            if label is None:
                self._log.write("[red]Snapshot not found.[/]")
                return
            self._log.write(f"[bold]Rolled back to:[/] {label}")
            self._calendar.refresh_view()

        self.push_screen(
            HistoryScreen(snaps, total, cap=self.store.max_snapshots),
            _on_pick,
        )

    def action_help(self) -> None:
        if self._log:
            self._log.write(HELP_TEXT)

    # ---- chat -----------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only act on submissions from the chat input itself — inputs inside
        # modals (e.g. the PromptModal for new tasks) bubble Input.Submitted
        # up to here too and would otherwise get sent to the chat.
        if event.input is not self._input:
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if self._busy:
            assert self._log is not None
            self._log.write("[yellow]Busy — wait for the current reply.[/]")
            return
        await self._run_chat(text)

    async def _run_chat(self, user_text: str) -> None:
        assert self._log is not None and self._calendar is not None
        assert self._status is not None
        self._busy = True
        self._status.start()
        self._log.write(f"[bold magenta]You:[/] {user_text}")

        async def on_text(chunk: str) -> None:
            self._log.write(f"[bold green]Claude:[/] {chunk}")

        async def on_tool(name: str, args: dict) -> None:
            short = name.replace("mcp__cal__", "")
            self._log.write(f"  [dim cyan]· {short}({_short_json(args)})[/]")

        result_seen = False

        async def on_result(message) -> None:
            nonlocal result_seen
            result_seen = True
            assert self._status is not None
            self._status.stop(
                summary=format_usage(
                    usage=getattr(message, "usage", None),
                    duration_ms=getattr(message, "duration_ms", None),
                )
            )

        try:
            await self.agent.send(
                user_text,
                view=self._calendar.mode,
                anchor=self._calendar.anchor,
                on_text=on_text,
                on_tool=on_tool,
                on_result=on_result,
            )
        except Exception as e:  # noqa: BLE001
            self._log.write(f"[red]Error:[/] {e}")
            self._status.stop(summary=f"[red]✗ {type(e).__name__}[/]")
        else:
            if not result_seen:
                self._status.stop()
        finally:
            self._busy = False
            self._calendar.refresh_view()
            if self._task_info is not None:
                self._task_info.refresh_panel()


def _short_json(obj: dict, limit: int = 80) -> str:
    s = json.dumps(obj, separators=(",", ":"))
    return s if len(s) <= limit else s[: limit - 1] + "…"
