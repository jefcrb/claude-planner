from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from planner.db import EventStore, Project, Task, tag_color


def _fmt_minutes(n: int) -> str:
    if n < 60:
        return f"{n}m"
    h, m = divmod(n, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


# ---- prompt sub-modal -------------------------------------------------


class PromptModal(ModalScreen[str | None]):
    """Tiny modal asking the user for one line of text."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, initial: str = "", placeholder: str = "") -> None:
        super().__init__()
        self._title = title
        self._initial = initial
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-dialog"):
            yield Static(f"[bold]{self._title}[/]", id="prompt-title")
            yield Input(value=self._initial, placeholder=self._placeholder, id="prompt-input")
            yield Static("[dim]Enter to confirm · Esc to cancel[/]", id="prompt-footer")

    def on_mount(self) -> None:
        inp = self.query_one("#prompt-input", Input)
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "no", "No"),
        Binding("n", "no", "No"),
        Binding("y", "yes", "Yes"),
        Binding("enter", "yes", "Yes"),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self._text)
            yield Static("[dim]y / Enter to confirm · n / Esc to cancel[/]", id="confirm-footer")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# ---- row records ------------------------------------------------------


@dataclass
class Row:
    kind: str  # 'project' | 'task'
    project: Project
    task: Task | None = None  # None for project rows


# ---- main modal -------------------------------------------------------


class ProjectsModal(ModalScreen[None]):
    """Tree-style list of projects with tasks, with action keys on the selected row."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("n", "new_task", "New task"),
        Binding("N", "new_project", "New project"),
        Binding("e", "rename", "Edit"),
        Binding("x", "delete", "Delete"),
        Binding("s", "start", "Start"),
        Binding("S", "stop_all", "Stop all"),
        Binding("c", "complete", "Complete"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self.store = store
        self._rows: list[Row] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="projects-dialog"):
            yield Static("", id="projects-header")
            yield OptionList(id="projects-list")
            yield Static(
                "[dim]n new task · N new project · e rename · x delete · "
                "s start · S stop-all · c complete · Esc close[/]",
                id="projects-footer",
            )

    def on_mount(self) -> None:
        self._refresh_list()

    # ---- rendering --------------------------------------------------

    def _build_rows(self) -> list[Row]:
        rows: list[Row] = []
        for proj in self.store.list_projects(include_archived=False):
            rows.append(Row(kind="project", project=proj))
            tasks = self.store.list_tasks(project_id=proj.id, include_done=True)
            for t in tasks:
                rows.append(Row(kind="task", project=proj, task=t))
        return rows

    def _render_row(self, row: Row) -> Text:
        line = Text()
        if row.kind == "project":
            line.append(row.project.name, style=f"bold {tag_color(row.project.name)}")
            n_tasks = sum(
                1 for r in self._rows
                if r.kind == "task" and r.project.id == row.project.id
            )
            total = self.store.total_minutes(project_id=row.project.id)
            line.append(
                f"   {n_tasks} task{'s' if n_tasks != 1 else ''} · "
                f"{_fmt_minutes(total)} tracked",
                style="dim",
            )
        else:
            t = row.task
            assert t is not None
            line.append("    ")
            ongoing = self.store.ongoing_session_for_task(t.id)
            if ongoing:
                line.append("▶ ", style="bold yellow")
            elif t.status == "done":
                line.append("✓ ", style="green")
            else:
                line.append("• ", style="dim")
            line.append(
                t.title,
                style="strike dim" if t.status == "done" else "default",
            )
            mins = self.store.total_minutes(task_id=t.id)
            if mins:
                line.append(f"  {_fmt_minutes(mins)}", style="cyan")
            if t.scheduled_for:
                line.append(
                    f"  @ {t.scheduled_for.strftime('%m/%d %H:%M')}", style="dim"
                )
            if t.estimate_minutes:
                line.append(
                    f"  est {_fmt_minutes(t.estimate_minutes)}", style="dim"
                )
            line.append(f"  #{t.id}", style="dim")
        return line

    def _refresh_list(self, *, preserve_cursor: bool = True) -> None:
        old_index = None
        ol = self.query_one("#projects-list", OptionList)
        if preserve_cursor:
            old_index = ol.highlighted

        self._rows = self._build_rows()
        ol.clear_options()
        for i, row in enumerate(self._rows):
            ol.add_option(Option(self._render_row(row), id=str(i)))

        n_projects = sum(1 for r in self._rows if r.kind == "project")
        n_tasks = sum(1 for r in self._rows if r.kind == "task")
        ongoing = self.store.list_sessions(ongoing_only=True)
        today_mins = self.store.total_minutes(
            on_date=datetime.now().date()
        )
        header = Text()
        header.append("Projects & Tasks", style="bold")
        header.append(
            f"   {n_projects} projects · {n_tasks} tasks · "
            f"{_fmt_minutes(today_mins)} today",
            style="dim",
        )
        if ongoing:
            header.append(f"   ▶ {len(ongoing)} ongoing", style="bold yellow")
        self.query_one("#projects-header", Static).update(header)

        if old_index is not None and self._rows:
            ol.highlighted = min(old_index, len(self._rows) - 1)

    # ---- selection helpers ------------------------------------------

    def _selected_row(self) -> Row | None:
        ol = self.query_one("#projects-list", OptionList)
        idx = ol.highlighted
        if idx is None or idx >= len(self._rows):
            return None
        return self._rows[idx]

    # ---- actions ----------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        self._refresh_list()

    def action_new_project(self) -> None:
        self.app.push_screen(
            PromptModal("New project", placeholder="project name"),
            self._after_new_project,
        )

    def action_new_task(self) -> None:
        row = self._selected_row()
        if row is None:
            # No project context — bounce to new-project so it's still useful.
            self.action_new_project()
            return
        self.app.push_screen(
            PromptModal(
                f"New task in '{row.project.name}'",
                placeholder="task title",
            ),
            lambda value: self._after_new_task(row.project, value),
        )

    def _after_new_project(self, name: str | None) -> None:
        if name:
            self.store.add_project(name)
            self._refresh_list()

    def _after_new_task(self, project: Project, title: str | None) -> None:
        if title:
            self.store.add_task(project.id, title)
            self._refresh_list()

    def action_rename(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        if row.kind == "project":
            self.app.push_screen(
                PromptModal("Rename project", initial=row.project.name),
                lambda val: self._after_rename_project(row.project, val),
            )
        else:
            assert row.task is not None
            self.app.push_screen(
                PromptModal("Edit task title", initial=row.task.title),
                lambda val: self._after_rename_task(row.task, val),  # type: ignore[arg-type]
            )

    def _after_rename_project(self, project: Project, name: str | None) -> None:
        if name and name != project.name:
            self.store.update_project(project.id, name=name)
            self._refresh_list()

    def _after_rename_task(self, task: Task, title: str | None) -> None:
        if title and title != task.title:
            self.store.update_task(task.id, title=title)
            self._refresh_list()

    def action_delete(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        if row.kind == "project":
            msg = (
                f"Delete project '[bold]{row.project.name}[/]' AND "
                "all its tasks and sessions?"
            )
            self.app.push_screen(
                ConfirmModal(msg),
                lambda ok: self._after_delete_project(row.project, ok),
            )
        else:
            assert row.task is not None
            msg = f"Delete task '[bold]{row.task.title}[/]'?"
            self.app.push_screen(
                ConfirmModal(msg),
                lambda ok: self._after_delete_task(row.task, ok),  # type: ignore[arg-type]
            )

    def _after_delete_project(self, project: Project, ok: bool) -> None:
        if ok:
            self.store.delete_project(project.id)
            self._refresh_list()

    def _after_delete_task(self, task: Task, ok: bool) -> None:
        if ok:
            self.store.delete_task(task.id)
            self._refresh_list()

    def action_start(self) -> None:
        row = self._selected_row()
        if row is None or row.kind != "task" or row.task is None:
            return
        existing = self.store.ongoing_session_for_task(row.task.id)
        if existing is not None:
            # Already running — stop it.
            self.store.stop_session(existing.id)
        else:
            self.store.start_session(row.task.id)
        self._refresh_list()

    def action_stop_all(self) -> None:
        n = self.store.stop_all_ongoing()
        if n:
            self._refresh_list()

    def action_complete(self) -> None:
        row = self._selected_row()
        if row is None or row.kind != "task" or row.task is None:
            return
        new_status = "open" if row.task.status == "done" else "done"
        self.store.update_task(row.task.id, status=new_status)
        self._refresh_list()
