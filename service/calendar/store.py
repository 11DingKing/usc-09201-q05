"""基于 SQLite 的追加式事件存储。

事件只增不改；同一 command_key 的重复提交返回首次产生的事件，
从存储层保证“重复回执保留原计划影响”。所有写入在单个事务中完成，
系统重启后事件与未决审批完整保留，提醒扫描只依据持久化事件重放。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import timezone

from .clock import to_iso
from .events import Event, EventType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS commands (
    command_key TEXT PRIMARY KEY,
    event_seqs TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class EventStore:
    """线程安全的追加式事件存储。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @classmethod
    def open(cls, path: str) -> "EventStore":
        """打开指定路径的存储。"""

        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        return cls(path)

    @property
    def path(self) -> str:
        return self._path

    def load_all(self) -> list[Event]:
        """读取全部事件（按序号）。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_type, occurred_at, actor, payload, idempotency_key "
                "FROM events ORDER BY seq"
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def peek_command(self, command_key: str) -> list[Event] | None:
        """返回某命令首次提交产生的事件；未见过该命令时返回 None。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT event_seqs FROM commands WHERE command_key = ?",
                (command_key,),
            ).fetchone()
            if row is None:
                return None
            seqs = [int(s) for s in json.loads(row["event_seqs"])]
            rows = self._conn.execute(
                "SELECT seq, event_type, occurred_at, actor, payload, idempotency_key "
                "FROM events WHERE seq IN (%s) ORDER BY seq" % ",".join("?" * len(seqs)),
                seqs,
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def append(
        self,
        new_events: list[tuple[EventType, object, str, dict, str | None]],
        command_key: str | None,
    ) -> tuple[list[Event], bool]:
        """原子追加一批事件。

        返回 (事件列表, 是否为首次提交)。command_key 已存在时，
        不写入任何新事件，直接返回首次提交的事件。
        """

        key = command_key or f"auto:{uuid.uuid4().hex}"
        with self._lock:
            existing = self._conn.execute(
                "SELECT event_seqs FROM commands WHERE command_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                seqs = [int(s) for s in json.loads(existing["event_seqs"])]
                placeholders = ",".join("?" * len(seqs))
                rows = self._conn.execute(
                    f"SELECT seq, event_type, occurred_at, actor, payload, idempotency_key "
                    f"FROM events WHERE seq IN ({placeholders}) ORDER BY seq",
                    seqs,
                ).fetchall()
                return [_row_to_event(row) for row in rows], False

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                seqs: list[int] = []
                for event_type, moment, actor, payload, idem in new_events:
                    cur = self._conn.execute(
                        "INSERT INTO events (event_type, occurred_at, actor, payload, idempotency_key) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            event_type.value,
                            to_iso(moment) if hasattr(moment, "tzinfo") else str(moment),
                            actor,
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            idem,
                        ),
                    )
                    seqs.append(int(cur.lastrowid))
                self._conn.execute(
                    "INSERT INTO commands (command_key, event_seqs, created_at) VALUES (?, ?, ?)",
                    (key, json.dumps(seqs), to_iso(_utc_now())),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            rows = self._conn.execute(
                "SELECT seq, event_type, occurred_at, actor, payload, idempotency_key "
                "FROM events WHERE seq IN (%s) ORDER BY seq" % ",".join("?" * len(seqs)),
                seqs,
            ).fetchall()
            return [_row_to_event(row) for row in rows], True

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _utc_now():
    from datetime import datetime

    return datetime.now(timezone.utc)


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=row["seq"],
        event_type=row["event_type"],
        occurred_at=row["occurred_at"],
        actor=row["actor"],
        payload=json.loads(row["payload"]),
        idempotency_key=row["idempotency_key"],
    )
