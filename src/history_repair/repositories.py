from __future__ import annotations

from typing import Any

from .db import SessionDatabase
from .models import MessageStatus
from .utils import new_id, now_ms


HIDDEN_THREAD_STATUSES = ("archived", "deleted", "removed")


def _to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class ThreadRepository:
    def __init__(self, db: SessionDatabase):
        self.db = db

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        return dict(row) if row else None

    def list_threads(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in HIDDEN_THREAD_STATUSES)
        rows = self.db.query_all(
            f"SELECT * FROM threads WHERE status NOT IN ({placeholders}) ORDER BY updated_at DESC",
            HIDDEN_THREAD_STATUSES,
        )
        return [dict(row) for row in rows]

    def create_thread(
        self,
        thread_id: str,
        title: str,
        *,
        status: str = "active",
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        ts = created_at_ms or now_ms()
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO threads (
                    thread_id, title, status, created_at, updated_at, last_message_at, last_continuation_error
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (thread_id, title, status, ts, ts, ts),
            )
        return self.get_thread(thread_id) or {}

    def touch_thread(self, thread_id: str, *, last_message_at: int | None = None) -> None:
        ts = now_ms()
        last_msg_ts = last_message_at or ts
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE threads
                SET updated_at = ?, last_message_at = ?
                WHERE thread_id = ?
                """,
                (ts, last_msg_ts, thread_id),
            )

    def set_last_continuation_error(self, thread_id: str, error: str | None) -> None:
        ts = now_ms()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE threads
                SET last_continuation_error = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (error, ts, thread_id),
            )


class MessageRepository:
    def __init__(self, db: SessionDatabase):
        self.db = db

    def next_seq(self, thread_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE thread_id = ?", (thread_id,))
        max_seq = int(row["max_seq"]) if row else 0
        return max_seq + 1

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM messages WHERE message_id = ?", (message_id,))
        return dict(row) if row else None

    def list_messages(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY seq ASC",
            (thread_id,),
        )
        return [dict(row) for row in rows]

    def list_context_messages(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.list_messages(thread_id)
        context_rows = []
        for row in rows:
            if row["role"] == "assistant" and row["status"] == MessageStatus.PENDING.value and not row["content"]:
                continue
            if row["role"] == "assistant" and row["status"] == MessageStatus.FAILED.value and not row["content"]:
                continue
            if row["status"] == MessageStatus.CANCELED.value and not row["content"]:
                continue
            context_rows.append(row)
        return context_rows

    def get_last_assistant(self, thread_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            """
            SELECT * FROM messages
            WHERE thread_id = ? AND role = 'assistant'
            ORDER BY seq DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        return dict(row) if row else None

    def create_message(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        status: MessageStatus,
        provider: str | None = None,
        account_id: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        ts = created_at_ms or now_ms()
        message_id = new_id("msg")
        seq = self.next_seq(thread_id)
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO messages (
                    message_id, thread_id, seq, role, content, status,
                    created_at, updated_at, provider, account_id, model, request_id,
                    error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    message_id,
                    thread_id,
                    seq,
                    role,
                    content,
                    status.value,
                    ts,
                    ts,
                    provider,
                    account_id,
                    model,
                    request_id,
                ),
            )
        return self.get_message(message_id) or {}

    def append_assistant_chunk(self, message_id: str, chunk: str) -> dict[str, Any] | None:
        if not chunk:
            return self.get_message(message_id)
        current = self.get_message(message_id)
        if not current:
            return None
        new_status = current["status"]
        if current["status"] == MessageStatus.PENDING.value:
            new_status = MessageStatus.STREAMING.value
        new_content = f"{current['content']}{chunk}"
        ts = now_ms()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE messages
                SET content = ?, status = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (new_content, new_status, ts, message_id),
            )
        return self.get_message(message_id)

    def set_status(
        self,
        message_id: str,
        status: MessageStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        ts = now_ms()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE messages
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (status.value, error_code, error_message, ts, message_id),
            )
        return self.get_message(message_id)

    def recover_incomplete_assistant_messages(
        self,
        *,
        now_ms_value: int | None = None,
        pending_timeout_ms: int = 60_000,
    ) -> None:
        ts = now_ms_value or now_ms()
        pending_deadline = ts - pending_timeout_ms
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE messages
                SET status = ?, updated_at = ?
                WHERE role = 'assistant' AND status = ?
                """,
                (MessageStatus.PARTIAL.value, ts, MessageStatus.STREAMING.value),
            )
            self.db.execute(
                """
                UPDATE messages
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE role = 'assistant'
                  AND status = ?
                  AND created_at <= ?
                  AND LENGTH(content) = 0
                """,
                (
                    MessageStatus.FAILED.value,
                    "PENDING_TIMEOUT",
                    "assistant message pending too long without content",
                    ts,
                    MessageStatus.PENDING.value,
                    pending_deadline,
                ),
            )


class RouteRepository:
    def __init__(self, db: SessionDatabase):
        self.db = db

    def create_route(
        self,
        *,
        thread_id: str,
        provider: str,
        account_id: str,
        model: str,
        continuation_mode: str,
        previous_response_id: str | None = None,
        remote_response_id: str | None = None,
        capabilities_snapshot: str | None = None,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        ts = created_at_ms or now_ms()
        route_id = new_id("route")
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO routes (
                    route_id, thread_id, provider, account_id, model,
                    previous_response_id, remote_response_id, continuation_mode,
                    capabilities_snapshot, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    thread_id,
                    provider,
                    account_id,
                    model,
                    previous_response_id,
                    remote_response_id,
                    continuation_mode,
                    capabilities_snapshot,
                    ts,
                ),
            )
        row = self.db.query_one("SELECT * FROM routes WHERE route_id = ?", (route_id,))
        return _to_dict(row)

    def get_latest_route(self, thread_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            """
            SELECT * FROM routes
            WHERE thread_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        return dict(row) if row else None

    def list_routes(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM routes WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        )
        return [dict(row) for row in rows]


class SummaryRepository:
    def __init__(self, db: SessionDatabase):
        self.db = db

    def create_summary(
        self,
        *,
        thread_id: str,
        source_start_seq: int,
        source_end_seq: int,
        summary_text: str,
        status: str,
        model: str,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        summary_id = new_id("summary")
        ts = created_at_ms or now_ms()
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO summaries (
                    summary_id, thread_id, source_start_seq, source_end_seq,
                    summary_text, status, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    thread_id,
                    source_start_seq,
                    source_end_seq,
                    summary_text,
                    status,
                    model,
                    ts,
                ),
            )
        row = self.db.query_one("SELECT * FROM summaries WHERE summary_id = ?", (summary_id,))
        return _to_dict(row)

    def list_summaries(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM summaries WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        )
        return [dict(row) for row in rows]
