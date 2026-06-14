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


# ---- dataclasses ------------------------------------------------------


@dataclass
class Event:
    id: int
    title: str
    start: datetime
    end: datetime
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    all_day: bool = False
    priority: str = "normal"
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
class Project:
    id: int
    name: str
    archived: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "archived": self.archived}

    @property
    def color(self) -> str:
        return tag_color(self.name)


@dataclass
class Task:
    id: int
    project_id: int
    title: str
    notes: str = ""
    status: str = "open"  # 'open' | 'done'
    scheduled_for: datetime | None = None
    estimate_minutes: int | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "notes": self.notes,
            "status": self.status,
            "scheduled_for": self.scheduled_for.isoformat(timespec="minutes")
            if self.scheduled_for
            else None,
            "estimate_minutes": self.estimate_minutes,
            "completed_at": self.completed_at.isoformat(timespec="minutes")
            if self.completed_at
            else None,
        }


@dataclass
class Session:
    id: int
    task_id: int
    start: datetime
    end: datetime | None
    notes: str = ""

    def duration_minutes(self, *, now: datetime | None = None) -> int:
        end = self.end or (now or datetime.now())
        return max(0, int((end - self.start).total_seconds() // 60))

    @property
    def is_ongoing(self) -> bool:
        return self.end is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes") if self.end else None,
            "notes": self.notes,
        }


@dataclass
class SnapshotInfo:
    id: int
    created_at: datetime
    label: str
    item_count: int
    size_bytes: int


# ---- helpers ----------------------------------------------------------


def _split_tags(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


def _join_tags(tags: Iterable[str]) -> str:
    return ",".join(t.strip().lower() for t in tags if t and t.strip())


def _opt_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ---- store ------------------------------------------------------------


class EventStore:
    """Holds events, projects, tasks, sessions, and snapshots for one DB file."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_db_path()
        self._conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
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

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                scheduled_for TEXT,
                estimate_minutes INTEGER,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS tasks_project_idx ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);
            CREATE INDEX IF NOT EXISTS tasks_scheduled_idx ON tasks(scheduled_for);

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                start TEXT NOT NULL,
                end   TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS sessions_task_idx ON sessions(task_id);
            CREATE INDEX IF NOT EXISTS sessions_start_idx ON sessions(start);
            CREATE INDEX IF NOT EXISTS sessions_ongoing_idx ON sessions(end) WHERE end IS NULL;
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
        n_total = (
            self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            + self._conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            + self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            + self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        )
        n_snaps = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        if n_total > 0 and n_snaps == 0:
            self._snapshot("baseline (pre-history)")

    def close(self) -> None:
        self._conn.close()

    # ---- row mapping ---------------------------------------------------

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

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"], name=row["name"], archived=bool(row["archived"])
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            notes=row["notes"] or "",
            status=row["status"] or "open",
            scheduled_for=_opt_dt(row["scheduled_for"]),
            estimate_minutes=row["estimate_minutes"],
            completed_at=_opt_dt(row["completed_at"]),
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            task_id=row["task_id"],
            start=datetime.fromisoformat(row["start"]),
            end=_opt_dt(row["end"]),
            notes=row["notes"] or "",
        )

    # ---- event queries (unchanged) -------------------------------------

    def list_range(self, start: datetime, end: datetime) -> list[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE start < ? AND end > ? ORDER BY all_day DESC, start",
            (end.isoformat(), start.isoformat()),
        )
        return [self._row_to_event(r) for r in cur.fetchall()]

    def list_day(self, d: date) -> list[Event]:
        start = datetime.combine(d, time.min)
        return self.list_range(start, start + timedelta(days=1))

    def list_week(self, monday: date) -> list[Event]:
        start = datetime.combine(monday, time.min)
        return self.list_range(start, start + timedelta(days=7))

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
        params: list = [end.isoformat(), start.isoformat()]
        sql = (
            "SELECT * FROM events WHERE start < ? AND end > ? "
            "AND all_day = 0 AND priority != 'low'"
        )
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY start"
        return [self._row_to_event(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- event mutations ------------------------------------------------

    def _insert_event(
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
        cols = "title, start, end, notes, tags, all_day, priority, series_id"
        vals = (
            title,
            start.isoformat(),
            end.isoformat(),
            notes,
            _join_tags(tags),
            1 if all_day else 0,
            priority,
            series_id,
        )
        if explicit_id is not None:
            self._conn.execute(
                f"INSERT INTO events(id, {cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (explicit_id, *vals),
            )
            new_id = explicit_id
        else:
            cur = self._conn.execute(
                f"INSERT INTO events({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                vals,
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
        ev = self._insert_event(
            title=title, start=start, end=end, notes=notes,
            tags=tags, all_day=all_day, priority=priority,
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
        cur_start, cur_end = start, end
        max_iter = 2000
        while cur_start.date() <= until and max_iter > 0:
            ev = self._insert_event(
                title=title, start=cur_start, end=cur_end, notes=notes,
                tags=tags, all_day=all_day, priority=priority, series_id=sid,
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
        new = existing
        if title is not None: new.title = title
        if start is not None: new.start = start
        if end is not None: new.end = end
        if notes is not None: new.notes = notes
        if tags is not None: new.tags = tags
        if all_day is not None: new.all_day = all_day
        if priority is not None: new.priority = priority
        self._conn.execute(
            "UPDATE events SET title=?, start=?, end=?, notes=?, "
            "tags=?, all_day=?, priority=? WHERE id=?",
            (
                new.title, new.start.isoformat(), new.end.isoformat(), new.notes,
                _join_tags(new.tags), 1 if new.all_day else 0, new.priority, event_id,
            ),
        )
        self._conn.commit()
        self._snapshot(f"Updated '{new.title}' (#{event_id})")
        return self.get(event_id)

    def delete(self, event_id: int) -> bool:
        existing = self.get(event_id)
        if existing is None:
            return False
        self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self._conn.commit()
        self._snapshot(f"Deleted '{existing.title}' (#{event_id})")
        return True

    def delete_series(self, series_id: str) -> int:
        events_ = self.list_series(series_id)
        if not events_:
            return 0
        title = events_[0].title
        for e in events_:
            self._conn.execute("DELETE FROM events WHERE id = ?", (e.id,))
        self._conn.commit()
        self._snapshot(f"Deleted series '{title}' ({len(events_)} instances)")
        return len(events_)

    # ---- project queries -----------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY archived, name COLLATE NOCASE"
        return [self._row_to_project(r) for r in self._conn.execute(sql).fetchall()]

    def get_project(self, project_id: int) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._row_to_project(row) if row else None

    def find_project_by_name(self, name: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return self._row_to_project(row) if row else None

    def ensure_project(self, name: str) -> Project:
        existing = self.find_project_by_name(name)
        if existing:
            return existing
        return self.add_project(name)

    # ---- project mutations ---------------------------------------------

    def add_project(self, name: str) -> Project:
        cur = self._conn.execute("INSERT INTO projects(name) VALUES (?)", (name,))
        self._conn.commit()
        proj = self.get_project(cur.lastrowid)  # type: ignore[arg-type]
        assert proj is not None
        self._snapshot(f"Added project '{name}'")
        return proj

    def update_project(
        self, project_id: int, *, name: str | None = None, archived: bool | None = None
    ) -> Project | None:
        existing = self.get_project(project_id)
        if existing is None:
            return None
        new_name = name if name is not None else existing.name
        new_arch = archived if archived is not None else existing.archived
        self._conn.execute(
            "UPDATE projects SET name=?, archived=? WHERE id=?",
            (new_name, 1 if new_arch else 0, project_id),
        )
        self._conn.commit()
        self._snapshot(f"Updated project '{new_name}' (#{project_id})")
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> bool:
        existing = self.get_project(project_id)
        if existing is None:
            return False
        # CASCADE removes tasks + sessions
        self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._conn.commit()
        self._snapshot(f"Deleted project '{existing.name}' (#{project_id})")
        return True

    # ---- task queries --------------------------------------------------

    def list_tasks(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
        scheduled_on: date | None = None,
        include_done: bool = True,
    ) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        elif not include_done:
            sql += " AND status != 'done'"
        if scheduled_on is not None:
            day_start = datetime.combine(scheduled_on, time.min).isoformat()
            day_end = datetime.combine(scheduled_on + timedelta(days=1), time.min).isoformat()
            sql += " AND scheduled_for >= ? AND scheduled_for < ?"
            params.extend([day_start, day_end])
        sql += " ORDER BY (scheduled_for IS NULL), scheduled_for, id"
        return [self._row_to_task(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_task(self, task_id: int) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    # ---- task mutations ------------------------------------------------

    def add_task(
        self,
        project_id: int,
        title: str,
        *,
        notes: str = "",
        scheduled_for: datetime | None = None,
        estimate_minutes: int | None = None,
    ) -> Task:
        cur = self._conn.execute(
            "INSERT INTO tasks(project_id, title, notes, scheduled_for, estimate_minutes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project_id,
                title,
                notes,
                scheduled_for.isoformat() if scheduled_for else None,
                estimate_minutes,
            ),
        )
        self._conn.commit()
        task = self.get_task(cur.lastrowid)  # type: ignore[arg-type]
        assert task is not None
        self._snapshot(f"Added task '{title}'")
        return task

    def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        status: str | None = None,
        project_id: int | None = None,
        scheduled_for: datetime | None = None,
        clear_scheduled: bool = False,
        estimate_minutes: int | None = None,
    ) -> Task | None:
        existing = self.get_task(task_id)
        if existing is None:
            return None
        new_title = title if title is not None else existing.title
        new_notes = notes if notes is not None else existing.notes
        new_status = status if status is not None else existing.status
        new_project = project_id if project_id is not None else existing.project_id
        if clear_scheduled:
            new_sched = None
        else:
            new_sched = scheduled_for if scheduled_for is not None else existing.scheduled_for
        new_est = estimate_minutes if estimate_minutes is not None else existing.estimate_minutes
        completed_at = existing.completed_at
        if new_status == "done" and existing.status != "done":
            completed_at = datetime.now().replace(microsecond=0)
        elif new_status != "done":
            completed_at = None
        self._conn.execute(
            "UPDATE tasks SET title=?, notes=?, status=?, project_id=?, "
            "scheduled_for=?, estimate_minutes=?, completed_at=? WHERE id=?",
            (
                new_title, new_notes, new_status, new_project,
                new_sched.isoformat() if new_sched else None,
                new_est,
                completed_at.isoformat() if completed_at else None,
                task_id,
            ),
        )
        self._conn.commit()
        self._snapshot(f"Updated task '{new_title}' (#{task_id})")
        return self.get_task(task_id)

    def complete_task(self, task_id: int) -> Task | None:
        return self.update_task(task_id, status="done")

    def delete_task(self, task_id: int) -> bool:
        existing = self.get_task(task_id)
        if existing is None:
            return False
        self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        self._snapshot(f"Deleted task '{existing.title}' (#{task_id})")
        return True

    # ---- session queries -----------------------------------------------

    def list_sessions(
        self,
        *,
        task_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        ongoing_only: bool = False,
    ) -> list[Session]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list = []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if ongoing_only:
            sql += " AND end IS NULL"
        if start is not None:
            sql += " AND (end IS NULL OR end > ?)"
            params.append(start.isoformat())
        if end is not None:
            sql += " AND start < ?"
            params.append(end.isoformat())
        sql += " ORDER BY start DESC"
        return [self._row_to_session(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_session(self, session_id: int) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def ongoing_session_for_task(self, task_id: int) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE task_id = ? AND end IS NULL "
            "ORDER BY start DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._row_to_session(row) if row else None

    def total_minutes(
        self,
        *,
        task_id: int | None = None,
        project_id: int | None = None,
        on_date: date | None = None,
    ) -> int:
        sql = (
            "SELECT s.start, COALESCE(s.end, datetime('now', 'localtime')) AS end "
            "FROM sessions s JOIN tasks t ON s.task_id = t.id WHERE 1=1"
        )
        params: list = []
        if task_id is not None:
            sql += " AND s.task_id = ?"
            params.append(task_id)
        if project_id is not None:
            sql += " AND t.project_id = ?"
            params.append(project_id)
        if on_date is not None:
            day_start = datetime.combine(on_date, time.min).isoformat()
            day_end = datetime.combine(on_date + timedelta(days=1), time.min).isoformat()
            sql += " AND s.start < ? AND COALESCE(s.end, datetime('now', 'localtime')) > ?"
            params.extend([day_end, day_start])
        total = 0
        now = datetime.now()
        for r in self._conn.execute(sql, params).fetchall():
            s = datetime.fromisoformat(r["start"])
            e = datetime.fromisoformat(r["end"]) if isinstance(r["end"], str) else now
            if on_date is not None:
                day_start_dt = datetime.combine(on_date, time.min)
                day_end_dt = day_start_dt + timedelta(days=1)
                s = max(s, day_start_dt)
                e = min(e, day_end_dt)
            total += max(0, int((e - s).total_seconds() // 60))
        return total

    # ---- session mutations ---------------------------------------------

    def start_session(
        self, task_id: int, *, start: datetime | None = None
    ) -> Session:
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"no task #{task_id}")
        s = (start or datetime.now()).replace(microsecond=0)
        cur = self._conn.execute(
            "INSERT INTO sessions(task_id, start) VALUES (?, ?)",
            (task_id, s.isoformat()),
        )
        self._conn.commit()
        sess = self.get_session(cur.lastrowid)  # type: ignore[arg-type]
        assert sess is not None
        self._snapshot(f"Started session on '{task.title}'")
        return sess

    def stop_session(
        self, session_id: int, *, end: datetime | None = None
    ) -> Session | None:
        existing = self.get_session(session_id)
        if existing is None or existing.end is not None:
            return existing
        e = (end or datetime.now()).replace(microsecond=0)
        self._conn.execute(
            "UPDATE sessions SET end = ? WHERE id = ?", (e.isoformat(), session_id)
        )
        self._conn.commit()
        task = self.get_task(existing.task_id)
        self._snapshot(
            f"Stopped session on '{task.title if task else f'#{existing.task_id}'}'"
        )
        return self.get_session(session_id)

    def stop_all_ongoing(self, end: datetime | None = None) -> int:
        e = (end or datetime.now()).replace(microsecond=0)
        cur = self._conn.execute(
            "UPDATE sessions SET end = ? WHERE end IS NULL", (e.isoformat(),)
        )
        self._conn.commit()
        if cur.rowcount:
            self._snapshot(f"Stopped {cur.rowcount} ongoing sessions")
        return cur.rowcount

    def add_session(
        self,
        task_id: int,
        start: datetime,
        end: datetime,
        *,
        notes: str = "",
    ) -> Session:
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"no task #{task_id}")
        cur = self._conn.execute(
            "INSERT INTO sessions(task_id, start, end, notes) VALUES (?, ?, ?, ?)",
            (task_id, start.isoformat(), end.isoformat(), notes),
        )
        self._conn.commit()
        sess = self.get_session(cur.lastrowid)  # type: ignore[arg-type]
        assert sess is not None
        self._snapshot(f"Logged session on '{task.title}'")
        return sess

    def update_session(
        self,
        session_id: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        clear_end: bool = False,
        notes: str | None = None,
    ) -> Session | None:
        existing = self.get_session(session_id)
        if existing is None:
            return None
        new_start = start if start is not None else existing.start
        if clear_end:
            new_end = None
        else:
            new_end = end if end is not None else existing.end
        new_notes = notes if notes is not None else existing.notes
        self._conn.execute(
            "UPDATE sessions SET start=?, end=?, notes=? WHERE id=?",
            (
                new_start.isoformat(),
                new_end.isoformat() if new_end else None,
                new_notes,
                session_id,
            ),
        )
        self._conn.commit()
        self._snapshot(f"Updated session #{session_id}")
        return self.get_session(session_id)

    def delete_session(self, session_id: int) -> bool:
        existing = self.get_session(session_id)
        if existing is None:
            return False
        self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._conn.commit()
        self._snapshot(f"Deleted session #{session_id}")
        return True

    # ---- snapshots / rollback ------------------------------------------

    def _all_state_payload(self) -> tuple[str, int]:
        events = [self._row_to_event(r).to_dict()
                  for r in self._conn.execute("SELECT * FROM events ORDER BY id")]
        projects = [self._row_to_project(r).to_dict()
                    for r in self._conn.execute("SELECT * FROM projects ORDER BY id")]
        tasks = [self._row_to_task(r).to_dict()
                 for r in self._conn.execute("SELECT * FROM tasks ORDER BY id")]
        sessions = [self._row_to_session(r).to_dict()
                    for r in self._conn.execute("SELECT * FROM sessions ORDER BY id")]
        payload = json.dumps(
            {"events": events, "projects": projects, "tasks": tasks, "sessions": sessions},
            separators=(",", ":"),
        )
        item_count = len(events) + len(projects) + len(tasks) + len(sessions)
        return payload, item_count

    def _snapshot(self, label: str) -> int:
        payload, count = self._all_state_payload()
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
                item_count=r["event_count"],
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
        row = self._conn.execute(
            "SELECT label, payload FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        # Backward-compat: legacy snapshots stored just a list of events.
        if isinstance(payload, list):
            data = {"events": payload, "projects": [], "tasks": [], "sessions": []}
        else:
            data = payload

        # Wipe everything sessions → tasks → projects (FK cascade respected)
        # and events independently.
        for tbl in ("sessions", "tasks", "projects", "events"):
            self._conn.execute(f"DELETE FROM {tbl}")
            self._conn.execute(
                "DELETE FROM sqlite_sequence WHERE name = ?", (tbl,)
            )

        for e in data.get("events", []):
            self._insert_event(
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
        for p in data.get("projects", []):
            self._conn.execute(
                "INSERT INTO projects(id, name, archived) VALUES (?, ?, ?)",
                (p["id"], p["name"], 1 if p.get("archived") else 0),
            )
        for t in data.get("tasks", []):
            self._conn.execute(
                "INSERT INTO tasks(id, project_id, title, notes, status, "
                "scheduled_for, estimate_minutes, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t["id"], t["project_id"], t["title"], t.get("notes", ""),
                    t.get("status", "open"),
                    t.get("scheduled_for"),
                    t.get("estimate_minutes"),
                    t.get("completed_at"),
                ),
            )
        for s in data.get("sessions", []):
            self._conn.execute(
                "INSERT INTO sessions(id, task_id, start, end, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    s["id"], s["task_id"], s["start"], s.get("end"), s.get("notes", ""),
                ),
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
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())
