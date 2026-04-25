from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import (  # noqa: E402
    DesktopHistoryAdapter,
    DesktopProviderConfig,
    MessageRepository,
    MessageStatus,
    SessionDatabase,
    StreamEvent,
    ThreadRepository,
)


class DesktopAdapterTests(unittest.TestCase):
    def _create_unsupported_schema_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO app_meta(key, value) VALUES ('schema_version', '999');
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
            ) VALUES ('thread_ro_adapter', 'Read Only Adapter', 'active', 1, 1, 1, NULL);
            """
        )
        conn.commit()
        conn.close()

    def test_startup_list_detail_and_send_use_desktop_payloads(self):
        class FakeProviderClient:
            requests = []

            def __init__(self, *, base_url: str, api_key: str, timeout_sec: float, extra_headers=None):
                self.base_url = base_url
                self.api_key = api_key
                self.timeout_sec = timeout_sec
                self.extra_headers = extra_headers

            def stream(self, request):
                FakeProviderClient.requests.append(request)
                yield StreamEvent(kind="delta", text="adapter answer")
                yield StreamEvent(kind="done", remote_response_id="resp_adapter")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter.db"
            with patch("history_repair.desktop_adapter.ResponsesApiProviderClient", FakeProviderClient):
                with DesktopHistoryAdapter(db_path) as adapter:
                    startup = adapter.startup()
                    send = adapter.send_text_message(
                        thread_id="thread_adapter",
                        message="hello adapter",
                        provider_config=DesktopProviderConfig(
                            provider="provider-a",
                            account_id="acc-1",
                            model="gpt-5.4",
                            base_url="https://example.test",
                            api_key="test-key",
                        ),
                    )
                    threads = adapter.list_threads()
                    detail = adapter.get_thread("thread_adapter")

            self.assertEqual(startup["status"], "ok")
            self.assertFalse(startup["read_only_mode"])
            self.assertTrue(startup["recover_executed"])
            self.assertEqual(send["status"], "ok")
            self.assertTrue(send["success"])
            self.assertEqual(send["remote_response_id"], "resp_adapter")
            self.assertIsNone(send["ui_notice"])
            self.assertEqual(len(FakeProviderClient.requests), 1)
            self.assertEqual(FakeProviderClient.requests[0].messages[0]["content"], "hello adapter")

            self.assertEqual(threads["count"], 1)
            self.assertEqual(threads["threads"][0]["thread_id"], "thread_adapter")
            self.assertEqual(threads["threads"][0]["latest_route"]["provider"], "provider-a")
            self.assertTrue(threads["threads"][0]["send_available"])
            self.assertEqual(threads["threads"][0]["last_message_preview"], "adapter answer")

            self.assertEqual(detail["status"], "ok")
            self.assertEqual(detail["thread"]["thread_id"], "thread_adapter")
            self.assertEqual(len(detail["messages"]), 2)
            self.assertEqual(detail["messages"][-1]["content"], "adapter answer")
            self.assertTrue(detail["send_available"])

    def test_adapter_startup_recovers_incomplete_messages_before_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "recover.db"
            db = SessionDatabase(db_path)
            db.apply_migrations()
            ThreadRepository(db).create_thread(thread_id="thread_recover", title="Recover")
            MessageRepository(db).create_message(
                thread_id="thread_recover",
                role="assistant",
                content="partial text",
                status=MessageStatus.STREAMING,
            )
            db.close()

            with DesktopHistoryAdapter(db_path) as adapter:
                adapter.startup()
                detail = adapter.get_thread("thread_recover")

            self.assertEqual(detail["messages"][0]["status"], "partial")
            self.assertEqual(detail["thread"]["last_message_status"], "partial")

    def test_adapter_payloads_include_last_continuation_error_after_failed_send(self):
        class FailingProviderClient:
            def __init__(self, *, base_url: str, api_key: str, timeout_sec: float, extra_headers=None):
                pass

            def stream(self, request):
                yield StreamEvent(kind="error", error_code="RATE_LIMIT", error_message="rate limited")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter_failed.db"
            with patch("history_repair.desktop_adapter.ResponsesApiProviderClient", FailingProviderClient):
                with DesktopHistoryAdapter(db_path) as adapter:
                    adapter.startup()
                    send = adapter.send_text_message(
                        thread_id="thread_adapter_failed",
                        message="hello",
                        provider_config=DesktopProviderConfig(
                            provider="provider-a",
                            account_id="acc-1",
                            model="gpt-5.4",
                            base_url="https://example.test",
                            api_key="test-key",
                        ),
                    )
                    threads = adapter.list_threads()
                    detail = adapter.get_thread("thread_adapter_failed")

            self.assertEqual(send["status"], "error")
            self.assertEqual(send["ui_notice"], "unable to continue thread: rate limited")
            self.assertEqual(
                threads["threads"][0]["last_continuation_error"],
                "[RATE_LIMIT] rate limited",
            )
            self.assertEqual(detail["last_continuation_error"], "[RATE_LIMIT] rate limited")
            self.assertEqual(detail["thread"]["last_continuation_error"], "[RATE_LIMIT] rate limited")

    def test_read_only_mode_returns_visible_threads_and_blocks_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported.db"
            self._create_unsupported_schema_db(db_path)

            with DesktopHistoryAdapter(db_path) as adapter:
                startup = adapter.startup()
                threads = adapter.list_threads()
                send = adapter.send_text_message(
                    thread_id="thread_ro_adapter",
                    message="hello",
                    provider_config=DesktopProviderConfig(
                        provider="provider-a",
                        account_id="acc-1",
                        model="gpt-5.4",
                        base_url="https://example.test",
                        api_key="test-key",
                    ),
                )

            self.assertEqual(startup["status"], "error")
            self.assertTrue(startup["read_only_mode"])
            self.assertEqual(threads["count"], 1)
            self.assertEqual(threads["threads"][0]["thread_id"], "thread_ro_adapter")
            self.assertFalse(threads["threads"][0]["send_available"])
            self.assertEqual(send["status"], "error")
            self.assertTrue(send["read_only_mode"])

    def test_read_only_mode_without_threads_table_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported_empty.db"
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
            conn.commit()
            conn.close()

            with DesktopHistoryAdapter(db_path) as adapter:
                startup = adapter.startup()
                threads = adapter.list_threads()
                detail = adapter.get_thread("missing-thread")

            self.assertEqual(startup["status"], "error")
            self.assertTrue(startup["read_only_mode"])
            self.assertEqual(threads["count"], 0)
            self.assertEqual(threads["threads"], [])
            self.assertEqual(detail["status"], "error")
            self.assertEqual(detail["error"], "thread not found")


if __name__ == "__main__":
    unittest.main()
