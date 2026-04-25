from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import (  # noqa: E402
    DesktopSessionHost,
    MessageRepository,
    MessageStatus,
    ProviderCapabilities,
    RouteTarget,
    SessionDatabase,
    StreamEvent,
    ThreadRepository,
)


class StaticProvider:
    def __init__(self, *, chunks: list[str], remote_response_id: str):
        self.chunks = chunks
        self.remote_response_id = remote_response_id

    def stream(self, request):
        for chunk in self.chunks:
            yield StreamEvent(kind="delta", text=chunk)
        yield StreamEvent(kind="done", remote_response_id=self.remote_response_id)


class FailingProvider:
    def stream(self, request):
        yield StreamEvent(kind="error", error_code="RATE_LIMIT", error_message="rate limited")


class HostIntegrationTests(unittest.TestCase):
    def _create_unsupported_schema_db(self, db_path: Path, *, with_thread: bool = False) -> None:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO app_meta(key, value) VALUES ('schema_version', '999');
            """
        )
        if with_thread:
            conn.executescript(
                """
                CREATE TABLE threads (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_message_at INTEGER NOT NULL,
                    last_continuation_error TEXT
                );
                INSERT INTO threads(
                    thread_id, title, status, created_at, updated_at, last_message_at, last_continuation_error
                ) VALUES ('thread_ro_host', 'Host ReadOnly', 'active', 1, 1, 1, NULL);
                """
            )
        conn.commit()
        conn.close()

    def test_startup_runs_recovery_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "startup.db"

            db = SessionDatabase(db_path)
            db.apply_migrations()
            thread_repo = ThreadRepository(db)
            message_repo = MessageRepository(db)
            thread_repo.create_thread(thread_id="thread_startup", title="Startup")
            pending = message_repo.create_message(
                thread_id="thread_startup",
                role="assistant",
                content="",
                status=MessageStatus.PENDING,
                created_at_ms=1,
            )
            streaming = message_repo.create_message(
                thread_id="thread_startup",
                role="assistant",
                content="chunk",
                status=MessageStatus.STREAMING,
                created_at_ms=1,
            )
            db.close()

            host = DesktopSessionHost(db_path)
            startup = host.startup(pending_timeout_ms=1)
            detail = host.get_thread_detail("thread_startup")
            pending_after = next(item for item in detail.messages if item["message_id"] == pending["message_id"])
            streaming_after = next(
                item for item in detail.messages if item["message_id"] == streaming["message_id"]
            )
            host.close()

            self.assertTrue(startup.success)
            self.assertFalse(startup.read_only_mode)
            self.assertTrue(startup.recover_executed)
            self.assertTrue(detail.send_available)
            self.assertIsNone(detail.send_unavailable_reason)
            self.assertEqual(pending_after["status"], "failed")
            self.assertEqual(streaming_after["status"], "partial")

    def test_startup_switches_to_read_only_on_migration_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported.db"
            self._create_unsupported_schema_db(db_path)

            host = DesktopSessionHost(db_path)
            startup = host.startup()
            host.close()

            self.assertFalse(startup.success)
            self.assertTrue(startup.read_only_mode)
            self.assertFalse(startup.recover_executed)
            self.assertIn("Unsupported schema_version", startup.migration_warning or "")
            self.assertIsNotNone(startup.migration_backup_path)

    def test_list_threads_includes_latest_route_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "list.db"
            host = DesktopSessionHost(db_path)
            host.startup()

            host.send_message(
                thread_id="thread_route",
                user_content="hello",
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5_000),
                provider_client=StaticProvider(chunks=["world"], remote_response_id="resp_1"),
            )
            rows = host.list_threads()
            host.close()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["thread_id"], "thread_route")
            self.assertEqual(rows[0]["latest_route"]["provider"], "provider-a")
            self.assertEqual(rows[0]["latest_route"]["account_id"], "acc-1")
            self.assertEqual(rows[0]["latest_route"]["model"], "gpt-5.4")
            self.assertTrue(rows[0]["send_available"])
            self.assertIsNone(rows[0]["send_unavailable_reason"])
            self.assertEqual(rows[0]["last_message_role"], "assistant")
            self.assertEqual(rows[0]["last_message_status"], "completed")
            self.assertEqual(rows[0]["last_message_preview"], "world")

    def test_send_returns_summary_notice_for_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "summary.db"
            host = DesktopSessionHost(db_path)
            host.startup()

            host.send_message(
                thread_id="thread_summary",
                user_content="A" * 120,
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5_000),
                provider_client=StaticProvider(chunks=["first"], remote_response_id="resp_1"),
            )
            result = host.send_message(
                thread_id="thread_summary",
                user_content="B" * 120,
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=80),
                provider_client=StaticProvider(chunks=["second"], remote_response_id="resp_2"),
            )
            host.close()

            self.assertTrue(result.send_result.success)
            self.assertEqual(result.send_result.continuation_mode.value, "summary_rebuild")
            self.assertEqual(result.ui_notice, "compressed earlier context to continue the thread")

    def test_list_and_detail_include_last_continuation_error_after_failed_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "failed_send.db"
            host = DesktopSessionHost(db_path)
            host.startup()

            result = host.send_message(
                thread_id="thread_failed",
                user_content="hello",
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5_000),
                provider_client=FailingProvider(),
            )
            rows = host.list_threads()
            detail = host.get_thread_detail("thread_failed")
            host.close()

            self.assertFalse(result.send_result.success)
            self.assertEqual(result.ui_notice, "unable to continue thread: rate limited")
            self.assertEqual(rows[0]["last_continuation_error"], "[RATE_LIMIT] rate limited")
            self.assertTrue(rows[0]["send_available"])
            self.assertIsNone(rows[0]["send_unavailable_reason"])
            self.assertEqual(detail.last_continuation_error, "[RATE_LIMIT] rate limited")

    def test_list_threads_marks_send_unavailable_in_read_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported_read_only.db"
            self._create_unsupported_schema_db(db_path, with_thread=True)

            host = DesktopSessionHost(db_path)
            startup = host.startup()
            rows = host.list_threads()
            host.close()

            self.assertTrue(startup.read_only_mode)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["thread_id"], "thread_ro_host")
            self.assertFalse(rows[0]["send_available"])
            self.assertEqual(rows[0]["send_unavailable_reason"], "database is in read-only recovery mode")

    def test_read_only_mode_without_threads_table_returns_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported_empty.db"
            self._create_unsupported_schema_db(db_path, with_thread=False)

            host = DesktopSessionHost(db_path)
            startup = host.startup()
            rows = host.list_threads()
            detail = host.get_thread_detail("missing-thread")
            host.close()

            self.assertTrue(startup.read_only_mode)
            self.assertEqual(rows, [])
            self.assertIsNone(detail)


if __name__ == "__main__":
    unittest.main()
