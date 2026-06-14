from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable


def default_db_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / "planner"
    p.mkdir(parents=True, exist_ok=True)
    return p / "planner.db"


def max_snapshots() -> int:
    raw = os.environ.get("PLANNER_MAX_SNAPSHOTS", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


# Stable palette of Rich-compatible color names.
TAG_PALETTE = [
    "cyan",
    "magenta",
    "yellow",
    "green",
    "bright_blue",
    "red",
    "bright_cyan",
    "bright_magenta",
]


def tag_color(tag: str) -> str:
    h = int(hashlib.md5(tag.lower().encode()).hexdigest(), 16)
    return TAG_PALETTE[h % len(TAG_PALETTE)]


@dataclass
class Event:
    id: int
    title: str
    start: datetime
    end: datetime
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    all_day: bool = False
    priority: str = "normal"  # 'normal' | 'low'
    series_id: str | None = None

    @property
    def primary_color(self) -> str:
        return tag_color(self.tags[0]) if self.tags else "white"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "notes": self.notes,
            "tags": self.tags,
            "all_day": self.all_day,
            "priority": self.priority,
            "series_id": self.series_id,
        }


@dataclass
class SnapshotInfo:
    id: int
    created_at: datetime
    label: str
    event_count: int
    size_bytes: int


def _split_tags(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


def _join_tags(tags: Iterable[str]) -> str:
    return ",".join(t.strip().lower() for t in tags if t and t.strip())


class EventStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_db_path()
        self._conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES)
        self._conn.row_factory = sqlite3.Row
        self.max_snapshots = max_snapshots()
        self._init()
        self._maybe_seed_baseline()

    # ---- schema --------------------------------------------------------

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start TEXT NOT NULL,
                end   TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS events_start_idx ON events(start);

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                label TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS snapshots_created_idx ON snapshots(created_at);
            """
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(events)")}
        adds = [
            ("tags", "TEXT NOT NULL DEFAULT ''"),
            ("all_day", "INTEGER NOT NULL DEFAULT 0"),
            ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
            ("series_id", "TEXT"),
        ]
        for col, decl in adds:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS events_series_idx ON events(series_id)"
        )

    def _maybe_seed_baseline(self) -> None:
        """If we have events but no snapshots, capture the pre-edit baseline."""
        n_events = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        n_snaps = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        if n_events > 0 and n_snaps == 0:
            self._snapshot("baseline (pre-history)")

    def close(self) -> None:
        self._conn.close()

    # ---- mapping -------------------------------------------------------

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            title=row["title"],
            start=datetime.fromisoformat(row["start"]),
            end=datetime.fromisoformat(row["end"]),
            notes=row["notes"] or "",
            tags=_split_tags(row["tags"] or ""),
            all_day=bool(row["all_day"]),
            priority=row["priority"] or "normal",
            series_id=row["series_id"],
        )

    # ---- queries -------------------------------------------------------

    def list_range(self, start: datetime, end: datetime) -> list[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE start < ? AND end > ? ORDER BY all_day DESC, start",
            (end.isoformat(), start.isoformat()),
        )
        return [self._row_to_event(r) for r in cur.fetchall()]

    def list_day(self, d: date) -> list[Event]:
        start = datetime.combine(d, time.min)
        end = start + timedelta(days=1)
        return self.list_range(start, end)

    def list_week(self, monday: date) -> list[Event]:
        start = datetime.combine(monday, time.min)
        end = start + timedelta(days=7)
        return self.list_range(start, end)

    def get(self, event_id: int) -> Event | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return self._row_to_event(row) if row else None

    def list_series(self, series_id: str) -> list[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE series_id = ? ORDER BY start", (series_id,)
        )
        return [self._row_to_event(r) for r in cur.fetchall()]

    def find_conflicts(
        self, start: datetime, end: datetime, exclude_id: int | None = None
    ) -> list[Event]:
        """Return non-low-priority, non-all-day events overlapping [start, end)."""
        params: list = [end.isoformat(), start.isoformat()]
        sql = (
            "SELECT * FROM events "
            "WHERE start < ? AND end > ? "
            "AND all_day = 0 AND priority != 'low'"
        )
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY start"
        cur = self._conn.execute(sql, params)
        return [self._row_to_event(r) for r in cur.fetchall()]

    def _all_events_payload(self) -> tuple[str, int]:
        cur = self._conn.execute("SELECT * FROM events ORDER BY id")
        rows = [self._row_to_event(r).to_dict() for r in cur.fetchall()]
        payload = json.dumps(rows, separators=(",", ":"))
        return payload, len(rows)

    # ---- mutations -----------------------------------------------------

    def _insert(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        notes: str = "",
        tags: Iterable[str] = (),
        all_day: bool = False,
        priority: str = "normal",
        series_id: str | None = None,
        explicit_id: int | None = None,
    ) -> Event:
        if explicit_id is not None:
            self._conn.execute(
                "INSERT INTO events(id, title, start, end, notes, tags, all_day, priority, series_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    explicit_id,
                    title,
                    start.isoformat(),
                    end.isoformat(),
                    notes,
                    _join_tags(tags),
                    1 if all_day else 0,
                    priority,
                    series_id,
                ),
            )
            new_id = explicit_id
        else:
            cur = self._conn.execute(
                "INSERT INTO events(title, start, end, notes, tags, all_day, priority, series_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    start.isoformat(),
                    end.isoformat(),
                    notes,
                    _join_tags(tags),
                    1 if all_day else 0,
                    priority,
                    series_id,
                ),
            )
            new_id = cur.lastrowid  # type: ignore[assignment]
        self._conn.commit()
        return self.get(new_id)  # type: ignore[return-value]

    def add(
        self,
        title: str,
        start: datetime,
        end: datetime,
        notes: str = "",
        tags: Iterable[str] = (),
        all_day: bool = False,
        priority: str = "normal",
    ) -> Event:
        ev = self._insert(
            title=title,
            start=start,
            end=end,
            notes=notes,
            tags=tags,
            all_day=all_day,
            priority=priority,
        )
        self._snapshot(f"Added '{title}'")
        return ev

    def add_recurring(
        self,
        title: str,
        start: datetime,
        end: datetime,
        recurrence: str,
        until: date,
        notes: str = "",
        tags: Iterable[str] = (),
        all_day: bool = False,
        priority: str = "normal",
    ) -> tuple[str, list[Event]]:
        sid = str(uuid.uuid4())
        instances = []
        cur_start = start
        cur_end = end
        max_iter = 2000
        while cur_start.date() <= until and max_iter > 0:
            ev = self._insert(
                title=title,
                start=cur_start,
                end=cur_end,
                notes=notes,
                tags=tags,
                all_day=all_day,
                priority=priority,
                series_id=sid,
            )
            instances.append(ev)
            cur_start, cur_end = _advance(cur_start, cur_end, recurrence)
            max_iter -= 1

        self._snapshot(
            f"Added series '{title}' ({recurrence}, {len(instances)} instances)"
        )
        return sid, instances

    def update(
        self,
        event_id: int,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        all_day: bool | None = None,
        priority: str | None = None,
    ) -> Event | None:
        existing = self.get(event_id)
        if existing is None:
            return None

        new_title = title if title is not None else existing.title
        new_start = start if start is not None else existing.start
        new_end = end if end is not None else existing.end
        new_notes = notes if notes is not None else existing.notes
        new_tags = tags if tags is not None else existing.tags
        new_all_day = all_day if all_day is not None else existing.all_day
        new_priority = priority if priority is not None else existing.priority

        self._conn.execute(
            "UPDATE events SET title=?, start=?, end=?, notes=?, "
            "tags=?, all_day=?, priority=? WHERE id=?",
            (
                new_title,
                new_start.isoformat(),
                new_end.isoformat(),
                new_notes,
                _join_tags(new_tags),
                1 if new_all_day else 0,
                new_priority,
                event_id,
            ),
        )
        self._conn.commit()
        self._snapshot(f"Updated '{new_title}' (#{event_id})")
        return self.get(event_id)

    def delete(self, event_id: int) -> bool:
        existing = self.get(event_id)
        if existing is None:
            return False
        self._delete_raw(event_id)
        self._snapshot(f"Deleted '{existing.title}' (#{event_id})")
        return True

    def delete_series(self, series_id: str) -> int:
        events_ = self.list_series(series_id)
        if not events_:
            return 0
        title = events_[0].title
        for e in events_:
            self._delete_raw(e.id)
        self._snapshot(f"Deleted series '{title}' ({len(events_)} instances)")
        return len(events_)

    def _delete_raw(self, event_id: int) -> None:
        self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self._conn.commit()

    # ---- snapshots / rollback -----------------------------------------

    def _snapshot(self, label: str) -> int:
        payload, count = self._all_events_payload()
        cur = self._conn.execute(
            "INSERT INTO snapshots(label, payload, size_bytes, event_count) "
            "VALUES (?, ?, ?, ?)",
            (label, payload, len(payload.encode("utf-8")), count),
        )
        self._conn.commit()
        self._evict_old_snapshots()
        return cur.lastrowid  # type: ignore[return-value]

    def _evict_old_snapshots(self) -> None:
        cap = self.max_snapshots
        n = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        if n <= cap:
            return
        self._conn.execute(
            "DELETE FROM snapshots WHERE id IN ("
            "SELECT id FROM snapshots ORDER BY id ASC LIMIT ?)",
            (n - cap,),
        )
        self._conn.commit()

    def list_snapshots(self) -> list[SnapshotInfo]:
        cur = self._conn.execute(
            "SELECT id, created_at, label, size_bytes, event_count "
            "FROM snapshots ORDER BY id DESC"
        )
        return [
            SnapshotInfo(
                id=r["id"],
                created_at=datetime.fromisoformat(r["created_at"]),
                label=r["label"],
                event_count=r["event_count"],
                size_bytes=r["size_bytes"],
            )
            for r in cur.fetchall()
        ]

    def total_snapshots_size(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM snapshots"
        ).fetchone()
        return int(row[0])

    def rollback(self, snapshot_id: int) -> str | None:
        """Restore the events table to the contents of a snapshot.

        Rollback itself is NOT snapshotted — the snapshots you skipped over
        remain in the history, so you can roll back to a later snapshot to
        recover the state you rolled away from.
        """
        row = self._conn.execute(
            "SELECT label, payload FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        events_ = json.loads(row["payload"])
        self._conn.execute("DELETE FROM events")
        # Reset autoincrement so future inserts don't collide.
        self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'events'")
        for e in events_:
            self._insert(
                title=e["title"],
                start=datetime.fromisoformat(e["start"]),
                end=datetime.fromisoformat(e["end"]),
                notes=e.get("notes", ""),
                tags=e.get("tags", []),
                all_day=bool(e.get("all_day", False)),
                priority=e.get("priority", "normal"),
                series_id=e.get("series_id"),
                explicit_id=e["id"],
            )
        self._conn.commit()
        return row["label"] or f"snapshot #{snapshot_id}"


def _advance(
    start: datetime, end: datetime, recurrence: str
) -> tuple[datetime, datetime]:
    r = recurrence.lower()
    if r == "daily":
        return start + timedelta(days=1), end + timedelta(days=1)
    if r == "weekly":
        return start + timedelta(weeks=1), end + timedelta(weeks=1)
    if r == "monthly":
        return _add_months(start, 1), _add_months(end, 1)
    if r == "yearly":
        return _add_months(start, 12), _add_months(end, 12)
    raise ValueError(f"unknown recurrence: {recurrence}")


def _add_months(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + n
    year = dt.year + m // 12
    month = m % 12 + 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())
