from __future__ import annotations

import time

from textual.widgets import Static

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TICK_INTERVAL = 0.1  # seconds


class StatusBar(Static):
    """Single-line status row: spinner + elapsed while busy; token usage when done."""

    def __init__(self) -> None:
        super().__init__("[dim]Ready[/]", id="status-bar")
        self._frame = 0
        self._timer = None
        self._busy = False
        self._started = 0.0

    def start(self) -> None:
        self._busy = True
        self._started = time.monotonic()
        self._frame = 0
        if self._timer is None:
            self._timer = self.set_interval(TICK_INTERVAL, self._tick)
        self._tick()

    def _tick(self) -> None:
        if not self._busy:
            return
        elapsed = time.monotonic() - self._started
        glyph = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        self._frame += 1
        self.update(f"[bold cyan]{glyph}[/] Claude is thinking…  [dim]{elapsed:0.1f}s[/]")

    def stop(self, *, summary: str | None = None) -> None:
        self._busy = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.update(summary or "[dim]Ready[/]")


def format_usage(
    *, usage: dict | None, duration_ms: int | None
) -> str:
    """Build a one-line summary of token usage and turn time."""
    parts: list[str] = ["[green]✓[/]"]
    u = usage or {}
    inp = int(u.get("input_tokens", 0))
    out = int(u.get("output_tokens", 0))
    cache_read = int(u.get("cache_read_input_tokens", 0))
    cache_write = int(u.get("cache_creation_input_tokens", 0))

    if inp or out or cache_read or cache_write:
        parts.append(f"{_fmt_count(inp)} in")
        parts.append(f"{_fmt_count(out)} out")
        if cache_read:
            parts.append(f"{_fmt_count(cache_read)} cached")
        if cache_write:
            parts.append(f"{_fmt_count(cache_write)} cache-write")
    if duration_ms:
        parts.append(f"{duration_ms / 1000:0.1f}s")

    return "  [dim]·[/]  ".join(parts) if len(parts) > 1 else "[dim]Ready[/]"


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
