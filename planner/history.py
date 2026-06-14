from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from planner.db import SnapshotInfo


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _fmt_snapshot_row(s: SnapshotInfo, *, now: datetime) -> Text:
    age = now - s.created_at
    if age.total_seconds() < 60:
        when = "just now"
    elif age.total_seconds() < 3600:
        when = f"{int(age.total_seconds() // 60)}m ago"
    elif age.days < 1:
        when = f"{int(age.total_seconds() // 3600)}h ago"
    else:
        when = s.created_at.strftime("%m/%d %H:%M")

    line = Text()
    line.append(f"{when:<12}", style="cyan")
    line.append("  ")
    line.append(s.label, style="bold")
    line.append("  ")
    line.append(
        f"· {s.event_count} event{'s' if s.event_count != 1 else ''} · {human_bytes(s.size_bytes)}",
        style="dim",
    )
    return line


class HistoryScreen(ModalScreen[int | None]):
    """Modal with a list of snapshots. Returns selected snapshot id or None."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("q", "cancel", "Close"),
    ]

    def __init__(
        self,
        snapshots: list[SnapshotInfo],
        total_size: int,
        cap: int,
    ):
        super().__init__()
        self._snapshots = snapshots
        self._total = total_size
        self._cap = cap

    def compose(self) -> ComposeResult:
        with Vertical(id="history-dialog"):
            header = Text()
            header.append("Version history", style="bold")
            header.append(
                f"   {len(self._snapshots)}/{self._cap} versions · "
                f"{human_bytes(self._total)} total",
                style="dim",
            )
            yield Static(header, id="history-header")

            now = datetime.now()
            options = [
                Option(_fmt_snapshot_row(s, now=now), id=str(s.id))
                for s in self._snapshots
            ]
            yield OptionList(*options, id="history-list")

            yield Static(
                "[dim]Enter rollback · ↑↓ navigate · Esc close[/]",
                id="history-footer",
            )

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_id is None:
            return
        self.dismiss(int(event.option_id))

    def action_cancel(self) -> None:
        self.dismiss(None)
