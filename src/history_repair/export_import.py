from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import SessionDatabase
from .utils import new_id


@dataclass(frozen=True)
class ImportReport:
    imported_threads: int
    imported_messages: int
    imported_routes: int
    imported_summaries: int
    thread_id_map: dict[str, str]


class ExportImportService:
    def __init__(self, db: SessionDatabase):
        self.db = db

    def export_jsonl(self, output_path: str | Path, *, thread_ids: list[str] | None = None) -> int:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        threads = self._load_threads(thread_ids=thread_ids)
        thread_id_values = [thread["thread_id"] for thread in threads]
        messages = self._load_messages(thread_ids=thread_id_values)
        routes = self._load_routes(thread_ids=thread_id_values)
        summaries = self._load_summaries(thread_ids=thread_id_values)

        count = 0
        with path.open("w", encoding="utf-8") as f:
            for row in threads:
                f.write(json.dumps({"type": "thread", "data": row}, ensure_ascii=False) + "\n")
                count += 1
            for row in messages:
                f.write(json.dumps({"type": "message", "data": row}, ensure_ascii=False) + "\n")
                count += 1
            for row in routes:
                f.write(json.dumps({"type": "route", "data": row}, ensure_ascii=False) + "\n")
                count += 1
            for row in summaries:
                f.write(json.dumps({"type": "summary", "data": row}, ensure_ascii=False) + "\n")
                count += 1
        return count

    def import_jsonl(self, input_path: str | Path) -> ImportReport:
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(str(path))

        buckets: dict[str, list[dict[str, Any]]] = {
            "thread": [],
            "message": [],
            "route": [],
            "summary": [],
        }
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue
                item = json.loads(raw)
                item_type = item.get("type")
                data = item.get("data")
                if item_type not in buckets:
                    raise ValueError(f"Invalid JSONL type at line {line_number}: {item_type}")
                if not isinstance(data, dict):
                    raise ValueError(f"Invalid JSONL payload at line {line_number}")
                self._validate_required_fields(item_type=item_type, data=data, line_number=line_number)
                buckets[item_type].append(data)

        thread_id_map: dict[str, str] = {}
        imported_threads = 0
        imported_messages = 0
        imported_routes = 0
        imported_summaries = 0

        with self.db.transaction():
            for thread in buckets["thread"]:
                source_thread_id = str(thread["thread_id"])
                target_thread_id = source_thread_id
                if self._thread_exists(source_thread_id):
                    target_thread_id = self._next_import_copy_thread_id(source_thread_id)
                thread_id_map[source_thread_id] = target_thread_id

                self.db.execute(
                    """
                    INSERT INTO threads (
                        thread_id, title, status, created_at, updated_at,
                        last_message_at, last_continuation_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_thread_id,
                        thread["title"],
                        thread["status"],
                        thread["created_at"],
                        thread["updated_at"],
                        thread["last_message_at"],
                        thread.get("last_continuation_error"),
                    ),
                )
                imported_threads += 1

            for message in buckets["message"]:
                source_thread_id = str(message["thread_id"])
                target_thread_id = thread_id_map.get(source_thread_id, source_thread_id)
                message_id = str(message["message_id"])
                if self._message_exists(message_id):
                    message_id = new_id("msg")
                self.db.execute(
                    """
                    INSERT INTO messages (
                        message_id, thread_id, seq, role, content, status,
                        created_at, updated_at, provider, account_id, model,
                        request_id, error_code, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        target_thread_id,
                        message["seq"],
                        message["role"],
                        message["content"],
                        message["status"],
                        message["created_at"],
                        message["updated_at"],
                        message.get("provider"),
                        message.get("account_id"),
                        message.get("model"),
                        message.get("request_id"),
                        message.get("error_code"),
                        message.get("error_message"),
                    ),
                )
                imported_messages += 1

            for route in buckets["route"]:
                source_thread_id = str(route["thread_id"])
                target_thread_id = thread_id_map.get(source_thread_id, source_thread_id)
                route_id = str(route["route_id"])
                if self._route_exists(route_id):
                    route_id = new_id("route")
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
                        target_thread_id,
                        route["provider"],
                        route["account_id"],
                        route["model"],
                        route.get("previous_response_id"),
                        route.get("remote_response_id"),
                        route["continuation_mode"],
                        route.get("capabilities_snapshot"),
                        route["created_at"],
                    ),
                )
                imported_routes += 1

            for summary in buckets["summary"]:
                source_thread_id = str(summary["thread_id"])
                target_thread_id = thread_id_map.get(source_thread_id, source_thread_id)
                summary_id = str(summary["summary_id"])
                if self._summary_exists(summary_id):
                    summary_id = new_id("summary")
                self.db.execute(
                    """
                    INSERT INTO summaries (
                        summary_id, thread_id, source_start_seq, source_end_seq,
                        summary_text, status, model, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id,
                        target_thread_id,
                        summary["source_start_seq"],
                        summary["source_end_seq"],
                        summary["summary_text"],
                        summary["status"],
                        summary["model"],
                        summary["created_at"],
                    ),
                )
                imported_summaries += 1

        return ImportReport(
            imported_threads=imported_threads,
            imported_messages=imported_messages,
            imported_routes=imported_routes,
            imported_summaries=imported_summaries,
            thread_id_map=thread_id_map,
        )

    def _load_threads(self, *, thread_ids: list[str] | None) -> list[dict[str, Any]]:
        if thread_ids is None:
            rows = self.db.query_all("SELECT * FROM threads ORDER BY created_at ASC")
            return [dict(row) for row in rows]

        if not thread_ids:
            return []
        placeholders = ",".join("?" for _ in thread_ids)
        rows = self.db.query_all(
            f"SELECT * FROM threads WHERE thread_id IN ({placeholders}) ORDER BY created_at ASC",
            tuple(thread_ids),
        )
        return [dict(row) for row in rows]

    def _load_messages(self, *, thread_ids: list[str]) -> list[dict[str, Any]]:
        if not thread_ids:
            return []
        placeholders = ",".join("?" for _ in thread_ids)
        rows = self.db.query_all(
            f"""
            SELECT * FROM messages
            WHERE thread_id IN ({placeholders})
            ORDER BY thread_id ASC, seq ASC
            """,
            tuple(thread_ids),
        )
        return [dict(row) for row in rows]

    def _load_routes(self, *, thread_ids: list[str]) -> list[dict[str, Any]]:
        if not thread_ids:
            return []
        placeholders = ",".join("?" for _ in thread_ids)
        rows = self.db.query_all(
            f"""
            SELECT * FROM routes
            WHERE thread_id IN ({placeholders})
            ORDER BY thread_id ASC, created_at ASC
            """,
            tuple(thread_ids),
        )
        return [dict(row) for row in rows]

    def _load_summaries(self, *, thread_ids: list[str]) -> list[dict[str, Any]]:
        if not thread_ids:
            return []
        placeholders = ",".join("?" for _ in thread_ids)
        rows = self.db.query_all(
            f"""
            SELECT * FROM summaries
            WHERE thread_id IN ({placeholders})
            ORDER BY thread_id ASC, created_at ASC
            """,
            tuple(thread_ids),
        )
        return [dict(row) for row in rows]

    def _validate_required_fields(self, *, item_type: str, data: dict[str, Any], line_number: int) -> None:
        required_fields_by_type: dict[str, set[str]] = {
            "thread": {
                "thread_id",
                "title",
                "status",
                "created_at",
                "updated_at",
                "last_message_at",
            },
            "message": {
                "message_id",
                "thread_id",
                "seq",
                "role",
                "content",
                "status",
                "created_at",
                "updated_at",
            },
            "route": {
                "route_id",
                "thread_id",
                "provider",
                "account_id",
                "model",
                "continuation_mode",
                "created_at",
            },
            "summary": {
                "summary_id",
                "thread_id",
                "source_start_seq",
                "source_end_seq",
                "summary_text",
                "status",
                "model",
                "created_at",
            },
        }
        required = required_fields_by_type[item_type]
        missing = [field for field in required if field not in data]
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise ValueError(f"Missing fields at line {line_number} for type {item_type}: {missing_text}")

    def _thread_exists(self, thread_id: str) -> bool:
        row = self.db.query_one("SELECT 1 FROM threads WHERE thread_id = ? LIMIT 1", (thread_id,))
        return row is not None

    def _message_exists(self, message_id: str) -> bool:
        row = self.db.query_one("SELECT 1 FROM messages WHERE message_id = ? LIMIT 1", (message_id,))
        return row is not None

    def _route_exists(self, route_id: str) -> bool:
        row = self.db.query_one("SELECT 1 FROM routes WHERE route_id = ? LIMIT 1", (route_id,))
        return row is not None

    def _summary_exists(self, summary_id: str) -> bool:
        row = self.db.query_one("SELECT 1 FROM summaries WHERE summary_id = ? LIMIT 1", (summary_id,))
        return row is not None

    def _next_import_copy_thread_id(self, source_thread_id: str) -> str:
        suffix = 1
        while True:
            candidate = f"{source_thread_id}_import_{suffix}"
            if not self._thread_exists(candidate):
                return candidate
            suffix += 1
