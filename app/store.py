"""SQLite persistence for immutable timetable revisions.

Every revision is stored as one row containing the complete table JSON.  A
merge therefore commits atomically: readers see either the previous revision
or the whole new revision, never a half-applied batch.  ``BEGIN IMMEDIATE``
serialises concurrent corrections across processes, and the merge is computed
against the latest committed row inside that write transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    revision        INTEGER PRIMARY KEY CHECK (revision >= 0),
    base_revision   INTEGER,
    parent_revision INTEGER,
    created_at      TEXT NOT NULL,
    trips_json      TEXT NOT NULL,
    details_json    TEXT NOT NULL
);
"""


class StoreError(Exception):
    pass


class StoreBusy(StoreError):
    pass


@dataclass(frozen=True)
class RevisionRecord:
    revision: int
    base_revision: Optional[int]
    parent_revision: Optional[int]
    created_at: str
    trips: list[dict]
    details: dict


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    # -- connection / schema -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            Path(self.path).expanduser().parent.mkdir(
                parents=True, exist_ok=True
            )
            last_error: Exception | None = None
            for attempt in range(12):
                conn: Optional[sqlite3.Connection] = None
                try:
                    conn = self._connect()
                    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                    mode = str(row[0]).lower() if row is not None else ""
                    if mode != "wal":
                        # Another process may hold the switch briefly.  Retry
                        # instead of silently continuing in rollback mode.
                        raise sqlite3.OperationalError(
                            "database is locked switching to WAL"
                        )
                    conn.execute(SCHEMA)
                    self._initialized = True
                    return
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    if "locked" not in str(exc).lower() and "busy" not in str(
                        exc
                    ).lower():
                        raise
                    time.sleep(min(0.05 * (attempt + 1), 0.5))
                finally:
                    if conn is not None:
                        conn.close()
            raise StoreBusy(f"无法初始化 SQLite 数据库: {last_error}")

    # -- reading -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RevisionRecord:
        return RevisionRecord(
            revision=row["revision"],
            base_revision=row["base_revision"],
            parent_revision=row["parent_revision"],
            created_at=row["created_at"],
            trips=json.loads(row["trips_json"]),
            details=json.loads(row["details_json"]),
        )

    def latest_conn(self, conn: sqlite3.Connection) -> Optional[RevisionRecord]:
        row = conn.execute(
            "SELECT revision, base_revision, parent_revision, created_at, "
            "trips_json, details_json FROM revisions "
            "ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_conn(
        self, conn: sqlite3.Connection, revision: int
    ) -> Optional[RevisionRecord]:
        row = conn.execute(
            "SELECT revision, base_revision, parent_revision, created_at, "
            "trips_json, details_json FROM revisions WHERE revision = ?",
            (revision,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def known_revisions_conn(self, conn: sqlite3.Connection) -> list[int]:
        rows = conn.execute(
            "SELECT revision FROM revisions ORDER BY revision"
        ).fetchall()
        return [row["revision"] for row in rows]

    def latest(self) -> Optional[RevisionRecord]:
        self.initialize()
        conn = self._connect()
        try:
            return self.latest_conn(conn)
        finally:
            conn.close()

    def get(self, revision: int) -> Optional[RevisionRecord]:
        self.initialize()
        conn = self._connect()
        try:
            return self.get_conn(conn, revision)
        finally:
            conn.close()

    def list_revisions(self) -> list[RevisionRecord]:
        self.initialize()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT revision, base_revision, parent_revision, created_at, "
                "trips_json, details_json FROM revisions ORDER BY revision"
            ).fetchall()
            return [self._row_to_record(row) for row in rows]
        finally:
            conn.close()

    # -- writing -------------------------------------------------------------

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Open an IMMEDIATE transaction, serialising writers.

        The write lock is acquired before the latest row is read, so every
        correction merges against the newest committed table.
        """

        self.initialize()
        conn: Optional[sqlite3.Connection] = None
        for attempt in range(10):
            candidate = self._connect()
            try:
                candidate.execute("BEGIN IMMEDIATE")
                conn = candidate
                break
            except sqlite3.OperationalError as exc:
                candidate.close()
                if "locked" not in str(exc).lower() and "busy" not in str(
                    exc
                ).lower():
                    raise
                time.sleep(min(0.05 * (attempt + 1), 0.5))
        if conn is None:
            raise StoreBusy("数据库写锁等待超时，请重试")

        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def insert_conn(
        conn: sqlite3.Connection,
        *,
        revision: int,
        base_revision: Optional[int],
        parent_revision: Optional[int],
        trips: list[dict],
        details: dict,
    ) -> RevisionRecord:
        created_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        conn.execute(
            "INSERT INTO revisions "
            "(revision, base_revision, parent_revision, created_at, "
            "trips_json, details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                revision,
                base_revision,
                parent_revision,
                created_at,
                json.dumps(trips, ensure_ascii=False, separators=(",", ":")),
                json.dumps(details, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return RevisionRecord(
            revision=revision,
            base_revision=base_revision,
            parent_revision=parent_revision,
            created_at=created_at,
            trips=trips,
            details=details,
        )
