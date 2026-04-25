from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any


SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_message_at INTEGER NOT NULL,
    last_continuation_error TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    provider TEXT,
    account_id TEXT,
    model TEXT,
    request_id TEXT,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
    UNIQUE (thread_id, seq)
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    model TEXT NOT NULL,
    previous_response_id TEXT,
    remote_response_id TEXT,
    continuation_mode TEXT NOT NULL,
    capabilities_snapshot TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    source_start_seq INTEGER NOT NULL,
    source_end_seq INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_updated_at ON threads(updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread_seq ON messages(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_thread_status ON messages(thread_id, status);
CREATE INDEX IF NOT EXISTS idx_routes_thread_created ON routes(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_summaries_thread_created ON summaries(thread_id, created_at);
"""


@dataclass(frozen=True)
class MigrationResult:
    success: bool
    read_only_mode: bool
    backup_path: str | None
    error: str | None


class SessionDatabase:
    def __init__(self, db_path: str | Path, *, readonly: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.read_only = bool(readonly)
        self.conn = self._open_connection(readonly=self.read_only)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        if not self.read_only:
            self.conn.execute("PRAGMA journal_mode = WAL;")

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _open_connection(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def switch_to_read_only(self) -> None:
        if self.read_only:
            return
        self.close()
        self.read_only = True
        self.conn = self._open_connection(readonly=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def apply_migrations(self) -> None:
        with self.transaction():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            row = self.query_one("SELECT value FROM app_meta WHERE key = 'schema_version'")
            if row is None:
                self.conn.executescript(SCHEMA_V1_SQL)
                self.execute(
                    "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                    ("schema_version", "1"),
                )
                self.execute(
                    "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                    ("data_format_version", "1"),
                )
                return

            version = int(row["value"])
            if version == 1:
                self.conn.executescript(SCHEMA_V1_SQL)
                return
            raise RuntimeError(f"Unsupported schema_version: {version}")

    def apply_migrations_with_backup(self) -> MigrationResult:
        backup_path: str | None = None
        schema_version = self._detect_schema_version()
        if schema_version is not None and schema_version != 1:
            backup_path = str(self._create_backup_snapshot())
        try:
            self.apply_migrations()
            return MigrationResult(
                success=True,
                read_only_mode=self.read_only,
                backup_path=backup_path,
                error=None,
            )
        except Exception as exc:
            self.switch_to_read_only()
            return MigrationResult(
                success=False,
                read_only_mode=self.read_only,
                backup_path=backup_path,
                error=str(exc),
            )

    def _detect_schema_version(self) -> int | None:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None
        try:
            row = self.query_one("SELECT value FROM app_meta WHERE key = 'schema_version'")
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    def _create_backup_snapshot(self) -> Path:
        timestamp = int(time.time() * 1000)
        backup_path = self.db_path.with_suffix(f"{self.db_path.suffix}.backup.{timestamp}")
        self.conn.commit()
        backup_conn = sqlite3.connect(backup_path)
        try:
            self.conn.backup(backup_conn)
        finally:
            backup_conn.close()
        wal_path = self.db_path.with_suffix(f"{self.db_path.suffix}-wal")
        shm_path = self.db_path.with_suffix(f"{self.db_path.suffix}-shm")
        if wal_path.exists():
            shutil.copy2(wal_path, Path(f"{backup_path}-wal"))
        if shm_path.exists():
            shutil.copy2(shm_path, Path(f"{backup_path}-shm"))
        return backup_path
