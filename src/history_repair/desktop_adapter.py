from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
from typing import Any

from .host import DesktopSessionHost, HostStartupResult
from .models import ProviderCapabilities, RouteTarget
from .providers import ResponsesApiProviderClient


@dataclass(frozen=True)
class DesktopProviderConfig:
    provider: str
    account_id: str
    model: str
    base_url: str
    api_key: str
    supports_previous_response_id: bool = True
    max_context_tokens: int = 120_000
    timeout_sec: float = 120.0
    extra_headers: dict[str, str] | None = None


class DesktopHistoryAdapter:
    def __init__(self, db_path: str | Path):
        self.host = DesktopSessionHost(db_path)
        self.startup_result: HostStartupResult | None = None

    def close(self) -> None:
        self.host.close()

    def __enter__(self) -> DesktopHistoryAdapter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def startup(self, *, pending_timeout_ms: int = 60_000) -> dict[str, Any]:
        self.startup_result = self.host.startup(pending_timeout_ms=pending_timeout_ms)
        return _startup_payload(self.startup_result)

    def list_threads(self, *, status: str | None = None) -> dict[str, Any]:
        self._ensure_started()
        threads = [self._thread_list_item(row) for row in self.host.list_threads(status=status)]
        return {"status": "ok", "threads": threads, "count": len(threads)}

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        self._ensure_started()
        detail = self.host.get_thread_detail(thread_id)
        if detail is None:
            return {"status": "error", "thread_id": thread_id, "error": "thread not found"}
        thread = dict(detail.thread)
        thread["last_continuation_error"] = detail.last_continuation_error
        return {
            "status": "ok",
            "thread": self._thread_list_item(thread),
            "messages": [self._message_item(message) for message in detail.messages],
            "routes": detail.routes,
            "summaries": detail.summaries,
            "send_available": detail.send_available,
            "send_unavailable_reason": detail.send_unavailable_reason,
            "last_continuation_error": detail.last_continuation_error,
        }

    def send_text_message(
        self,
        *,
        thread_id: str,
        message: str,
        provider_config: DesktopProviderConfig,
        thread_title: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_started()
        if self.host.db.read_only:
            return {
                "status": "error",
                "thread_id": thread_id,
                "error": "database is in read-only recovery mode",
                "read_only_mode": True,
            }
        provider_client = ResponsesApiProviderClient(
            base_url=provider_config.base_url,
            api_key=provider_config.api_key,
            timeout_sec=provider_config.timeout_sec,
            extra_headers=provider_config.extra_headers,
        )
        host_result = self.host.send_message(
            thread_id=thread_id,
            user_content=message,
            route_target=RouteTarget(
                provider=provider_config.provider,
                account_id=provider_config.account_id,
                model=provider_config.model,
            ),
            capabilities=ProviderCapabilities(
                supports_previous_response_id=provider_config.supports_previous_response_id,
                max_context_tokens=provider_config.max_context_tokens,
            ),
            provider_client=provider_client,
            thread_title=thread_title,
            instructions=instructions,
        )
        result = host_result.send_result
        return {
            "status": "ok" if result.success else "error",
            "thread_id": result.thread_id,
            "user_message_id": result.user_message_id,
            "assistant_message_id": result.assistant_message_id,
            "success": result.success,
            "continuation_mode": result.continuation_mode.value,
            "remote_response_id": result.remote_response_id,
            "summary_id": result.summary_id,
            "error": result.error,
            "error_code": result.error_code,
            "ui_notice": host_result.ui_notice,
        }

    def export_jsonl(self, output_path: str | Path, *, thread_ids: list[str] | None = None) -> dict[str, Any]:
        self._ensure_started()
        count = self.host.export_jsonl(output_path=output_path, thread_ids=thread_ids)
        return {
            "status": "ok",
            "output": str(Path(output_path)),
            "records_written": count,
            "thread_ids": thread_ids or [],
        }

    def import_jsonl(self, input_path: str | Path) -> dict[str, Any]:
        self._ensure_started()
        if self.host.db.read_only:
            return {
                "status": "error",
                "input": str(Path(input_path)),
                "error": "database is in read-only recovery mode",
                "read_only_mode": True,
            }
        report = self.host.import_jsonl(input_path)
        return {"status": "ok", "input": str(Path(input_path)), **asdict(report)}

    def _ensure_started(self) -> None:
        if self.startup_result is None:
            self.startup()

    def _thread_list_item(self, row: dict[str, Any]) -> dict[str, Any]:
        send_available, send_unavailable_reason = self.host._build_send_availability(row)
        latest_route = row.get("latest_route")
        if latest_route is None:
            try:
                latest_route = self.host.route_repo.get_latest_route(str(row["thread_id"]))
            except sqlite3.OperationalError:
                latest_route = None
        last_message_role = row.get("last_message_role")
        last_message_status = row.get("last_message_status")
        last_message_preview = row.get("last_message_preview")
        if last_message_role is None and last_message_status is None and last_message_preview is None:
            try:
                messages = self.host.message_repo.list_messages(str(row["thread_id"]))
            except sqlite3.OperationalError:
                messages = []
            last_message = messages[-1] if messages else None
            if last_message:
                last_message_role = last_message["role"]
                last_message_status = last_message["status"]
                last_message_preview = self.host._message_preview(last_message)
        return {
            "thread_id": row["thread_id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_message_at": row["last_message_at"],
            "last_continuation_error": row.get("last_continuation_error"),
            "latest_route": latest_route,
            "send_available": send_available,
            "send_unavailable_reason": send_unavailable_reason,
            "last_message_role": last_message_role,
            "last_message_status": last_message_status,
            "last_message_preview": last_message_preview,
        }

    def _message_item(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "seq": row["seq"],
            "role": row["role"],
            "content": row["content"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "provider": row.get("provider"),
            "account_id": row.get("account_id"),
            "model": row.get("model"),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
        }


def _startup_payload(result: HostStartupResult) -> dict[str, Any]:
    return {
        "status": "ok" if result.success else "error",
        "read_only_mode": result.read_only_mode,
        "migration_warning": result.migration_warning,
        "migration_backup_path": result.migration_backup_path,
        "recover_executed": result.recover_executed,
    }
