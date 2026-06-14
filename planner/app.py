from __future__ import annotations

import json
from datetime import date, timedelta

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from planner.agent import PlannerAgent
from planner.db import EventStore, week_start
from planner.history import HistoryScreen
from planner.status import StatusBar, format_usage
from planner.views import render_day, render_week

HELP_TEXT = (
    "[bold]Hotkeys[/bold]  "
    "[cyan]w[/]=week  [cyan]d[/]=day  "
    "[cyan]←[/] prev  [cyan]→[/] next  "
    "[cyan]t[/]=today  [cyan]i[/]=focus chat  "
    "[cyan]Esc[/]=unfocus chat  [cyan]h[/]=history  "
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
            events_ = self.store.list_week(week_start(self.anchor))
            self.update(render_week(self.anchor, events_))
        else:
            events_ = self.store.list_day(self.anchor)
            self.update(render_day(self.anchor, events_))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.refresh_view()

    def step(self, direction: int) -> None:
        days = 7 if self.mode == "week" else 1
        self.anchor = self.anchor + timedelta(days=direction * days)
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
        Binding("left", "prev", "Prev"),
        Binding("right", "next", "Next"),
        Binding("t", "today", "Today"),
        Binding("i", "focus_chat", "Chat"),
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
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._calendar = CalendarView(self.store)
        yield self._calendar
        with Vertical(id="chat"):
            self._log = RichLog(id="chat-log", markup=True, wrap=True, highlight=False)
            self._log.can_focus = False
            yield self._log
            self._status = StatusBar()
            yield self._status
            self._input = ChatInput(
                placeholder="Ask anything — 'add focus block tomorrow 9-11', 'plan my Tuesday'...",
                id="chat-input",
            )
            yield self._input
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

    def action_prev(self) -> None:
        if self._calendar:
            self._calendar.step(-1)

    def action_next(self) -> None:
        if self._calendar:
            self._calendar.step(+1)

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


def _short_json(obj: dict, limit: int = 80) -> str:
    s = json.dumps(obj, separators=(",", ":"))
    return s if len(s) <= limit else s[: limit - 1] + "…"
