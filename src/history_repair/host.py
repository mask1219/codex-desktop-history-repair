from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from .db import SessionDatabase
from .export_import import ExportImportService, ImportReport
from .models import ProviderCapabilities, ProviderClient, RouteTarget
from .planner import ContinuationPlanner
from .repositories import MessageRepository, RouteRepository, SummaryRepository, ThreadRepository
from .service import ChatSessionService, SendResult
from .streaming import StreamWriter
from .summary import SummaryManager


@dataclass(frozen=True)
class HostStartupResult:
    success: bool
    read_only_mode: bool
    migration_warning: str | None
    migration_backup_path: str | None
    recover_executed: bool


@dataclass(frozen=True)
class ThreadDetail:
    thread: dict[str, Any]
    messages: list[dict[str, Any]]
    routes: list[dict[str, Any]]
    summaries: list[dict[str, Any]]
    send_available: bool
    send_unavailable_reason: str | None
    last_continuation_error: str | None


@dataclass(frozen=True)
class HostSendResult:
    send_result: SendResult
    ui_notice: str | None


class DesktopSessionHost:
    def __init__(self, db_path: str | Path):
        self.db = SessionDatabase(Path(db_path))
        self.thread_repo = ThreadRepository(self.db)
        self.message_repo = MessageRepository(self.db)
        self.route_repo = RouteRepository(self.db)
        self.summary_repo = SummaryRepository(self.db)
        self.service = ChatSessionService(
            thread_repo=self.thread_repo,
            message_repo=self.message_repo,
            route_repo=self.route_repo,
            planner=ContinuationPlanner(),
            summary_manager=SummaryManager(self.summary_repo),
            stream_writer=StreamWriter(self.message_repo),
        )
        self.export_import = ExportImportService(self.db)

    def close(self) -> None:
        self.db.close()

    def startup(self, *, pending_timeout_ms: int = 60_000) -> HostStartupResult:
        migration = self.db.apply_migrations_with_backup()
        recover_executed = False
        if not self.db.read_only:
            self.service.recover_incomplete_messages(pending_timeout_ms=pending_timeout_ms)
            recover_executed = True
        return HostStartupResult(
            success=migration.success,
            read_only_mode=self.db.read_only,
            migration_warning=migration.error,
            migration_backup_path=migration.backup_path,
            recover_executed=recover_executed,
        )

    def list_threads(self, *, status: str | None = None) -> list[dict[str, Any]]:
        try:
            threads = self.thread_repo.list_threads()
        except sqlite3.OperationalError:
            return []
        if status:
            threads = [item for item in threads if str(item.get("status")) == status]
        result: list[dict[str, Any]] = []
        for thread in threads:
            latest_route = None
            try:
                latest_route = self.route_repo.get_latest_route(str(thread["thread_id"]))
            except sqlite3.OperationalError:
                latest_route = None
            last_message = None
            try:
                last_message = self.message_repo.list_messages(str(thread["thread_id"]))[-1]
            except IndexError:
                last_message = None
            except sqlite3.OperationalError:
                last_message = None
            send_available, send_unavailable_reason = self._build_send_availability(thread)
            last_continuation_error = self._build_last_continuation_error(thread, last_message)
            row = dict(thread)
            row["latest_route"] = latest_route
            row["send_available"] = send_available
            row["send_unavailable_reason"] = send_unavailable_reason
            row["last_continuation_error"] = last_continuation_error
            row["last_message_role"] = last_message["role"] if last_message else None
            row["last_message_status"] = last_message["status"] if last_message else None
            row["last_message_preview"] = self._message_preview(last_message) if last_message else None
            result.append(row)
        return result

    def get_thread_detail(self, thread_id: str) -> ThreadDetail | None:
        try:
            thread = self.thread_repo.get_thread(thread_id)
        except sqlite3.OperationalError:
            return None
        if not thread:
            return None
        messages: list[dict[str, Any]]
        routes: list[dict[str, Any]]
        summaries: list[dict[str, Any]]
        try:
            messages = self.message_repo.list_messages(thread_id)
        except sqlite3.OperationalError:
            messages = []
        try:
            routes = self.route_repo.list_routes(thread_id)
        except sqlite3.OperationalError:
            routes = []
        try:
            summaries = self.summary_repo.list_summaries(thread_id)
        except sqlite3.OperationalError:
            summaries = []
        send_available, send_unavailable_reason = self._build_send_availability(thread)
        last_message = messages[-1] if messages else None
        last_continuation_error = self._build_last_continuation_error(thread, last_message)
        return ThreadDetail(
            thread=thread,
            messages=messages,
            routes=routes,
            summaries=summaries,
            send_available=send_available,
            send_unavailable_reason=send_unavailable_reason,
            last_continuation_error=last_continuation_error,
        )

    def send_message(
        self,
        *,
        thread_id: str,
        user_content: str,
        route_target: RouteTarget,
        capabilities: ProviderCapabilities,
        provider_client: ProviderClient,
        thread_title: str | None = None,
        instructions: str | None = None,
    ) -> HostSendResult:
        if self.db.read_only:
            raise RuntimeError("database is in read-only recovery mode")
        send_result = self.service.send_message(
            thread_id=thread_id,
            user_content=user_content,
            route_target=route_target,
            capabilities=capabilities,
            provider_client=provider_client,
            thread_title=thread_title,
            instructions=instructions,
        )
        return HostSendResult(
            send_result=send_result,
            ui_notice=self._build_ui_notice(send_result),
        )

    def export_jsonl(self, output_path: str | Path, *, thread_ids: list[str] | None = None) -> int:
        return self.export_import.export_jsonl(output_path=output_path, thread_ids=thread_ids)

    def import_jsonl(self, input_path: str | Path) -> ImportReport:
        if self.db.read_only:
            raise RuntimeError("database is in read-only recovery mode")
        return self.export_import.import_jsonl(input_path=input_path)

    def _build_ui_notice(self, send_result: SendResult) -> str | None:
        if send_result.continuation_note:
            return send_result.continuation_note
        if send_result.success and send_result.summary_id:
            return "compressed earlier context to continue the thread"
        if not send_result.success and send_result.error:
            return f"unable to continue thread: {send_result.error}"
        return None

    def _build_send_availability(self, thread: dict[str, Any]) -> tuple[bool, str | None]:
        if self.db.read_only:
            return False, "database is in read-only recovery mode"
        thread_status = str(thread.get("status", "active"))
        if thread_status != "active":
            return False, f"thread status is {thread_status}"
        return True, None

    def _build_last_continuation_error(
        self,
        thread: dict[str, Any],
        last_message: dict[str, Any] | None,
    ) -> str | None:
        thread_error = thread.get("last_continuation_error")
        if thread_error:
            return str(thread_error)
        if not last_message or last_message.get("role") != "assistant":
            return None
        status = str(last_message.get("status", ""))
        if status not in {"failed", "partial", "canceled"}:
            return None
        error_message = last_message.get("error_message")
        error_code = last_message.get("error_code")
        if error_message:
            if error_code:
                return f"[{error_code}] {error_message}"
            return str(error_message)
        if status == "failed":
            return "last assistant response failed"
        if status == "partial":
            return "last assistant response was interrupted"
        return "last assistant response was canceled"

    def _message_preview(self, message: dict[str, Any]) -> str:
        content = str(message.get("content", "")).strip().replace("\n", " ")
        if len(content) <= 120:
            return content
        return f"{content[:120]}..."
