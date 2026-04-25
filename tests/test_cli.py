from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import MessageRepository, MessageStatus, SessionDatabase, StreamEvent, ThreadRepository  # noqa: E402
from history_repair.cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    def _run_cli(self, args: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(args)
        output = buffer.getvalue().strip()
        payload = json.loads(output) if output else {}
        return code, payload

    def _create_unsupported_schema_db(self, db_path: Path, *, with_thread: bool) -> None:
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
                ) VALUES ('thread_ro', 'ReadOnly Thread', 'active', 1, 1, 1, NULL);
                """
            )
        conn.commit()
        conn.close()

    def test_init_export_import_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_db_path = tmp_path / "source.db"
            target_db_path = tmp_path / "target.db"
            export_path = tmp_path / "out.jsonl"

            code, payload = self._run_cli(["init", "--db", str(source_db_path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["command"], "init")
            self.assertEqual(payload["schema_version"], "1")

            source_db = SessionDatabase(source_db_path)
            source_db.apply_migrations()
            thread_repo = ThreadRepository(source_db)
            message_repo = MessageRepository(source_db)
            thread_repo.create_thread(thread_id="thread_cli", title="CLI Thread")
            message_repo.create_message(
                thread_id="thread_cli",
                role="assistant",
                content="",
                status=MessageStatus.PENDING,
                created_at_ms=1,
            )
            source_db.close()

            code, payload = self._run_cli(
                [
                    "recover",
                    "--db",
                    str(source_db_path),
                    "--pending-timeout-ms",
                    "1",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["command"], "recover")

            code, payload = self._run_cli(
                ["export", "--db", str(source_db_path), "--output", str(export_path)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["command"], "export")
            self.assertGreaterEqual(payload["records_written"], 2)

            code, payload = self._run_cli(
                ["import", "--db", str(target_db_path), "--input", str(export_path)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["command"], "import")
            self.assertEqual(payload["imported_threads"], 1)
            self.assertEqual(payload["imported_messages"], 1)

    def test_send_command_uses_provider_and_persists_messages(self):
        class FakeProviderClient:
            requests = []

            def __init__(self, *, base_url: str, api_key: str, timeout_sec: float):
                self.base_url = base_url
                self.api_key = api_key
                self.timeout_sec = timeout_sec

            def stream(self, request):
                FakeProviderClient.requests.append(request)
                index = len(FakeProviderClient.requests)
                yield StreamEvent(kind="delta", text=f"assistant-{index}")
                yield StreamEvent(kind="done", remote_response_id=f"resp_{index}")

        with tempfile.TemporaryDirectory() as tmp:
            FakeProviderClient.requests = []
            tmp_path = Path(tmp)
            db_path = tmp_path / "send.db"

            with patch("history_repair.cli.ResponsesApiProviderClient", FakeProviderClient):
                code1, payload1 = self._run_cli(
                    [
                        "send",
                        "--db",
                        str(db_path),
                        "--thread-id",
                        "thread_send",
                        "--message",
                        "first question",
                        "--provider",
                        "provider-a",
                        "--account-id",
                        "acc-1",
                        "--model",
                        "gpt-5.4",
                        "--base-url",
                        "https://example.test",
                        "--api-key",
                        "test-key",
                    ]
                )
                code2, payload2 = self._run_cli(
                    [
                        "send",
                        "--db",
                        str(db_path),
                        "--thread-id",
                        "thread_send",
                        "--message",
                        "second question",
                        "--provider",
                        "provider-a",
                        "--account-id",
                        "acc-1",
                        "--model",
                        "gpt-5.4",
                        "--base-url",
                        "https://example.test",
                        "--api-key",
                        "test-key",
                    ]
                )

            self.assertEqual(code1, 0)
            self.assertEqual(code2, 0)
            self.assertEqual(payload1["command"], "send")
            self.assertEqual(payload2["command"], "send")
            self.assertTrue(payload1["success"])
            self.assertTrue(payload2["success"])
            self.assertIn("ui_notice", payload1)
            self.assertIn("ui_notice", payload2)
            self.assertEqual(payload1["continuation_mode"], "local_rebuild")
            self.assertEqual(payload2["continuation_mode"], "remote_chain")

            self.assertEqual(len(FakeProviderClient.requests), 2)
            self.assertIsNone(FakeProviderClient.requests[0].previous_response_id)
            self.assertEqual(FakeProviderClient.requests[1].previous_response_id, "resp_1")
            self.assertEqual(len(FakeProviderClient.requests[1].messages), 1)
            self.assertEqual(FakeProviderClient.requests[1].messages[0]["content"], "second question")

            db = SessionDatabase(db_path)
            db.apply_migrations()
            messages = MessageRepository(db).list_messages("thread_send")
            self.assertEqual(len(messages), 4)
            self.assertEqual(messages[-1]["role"], "assistant")
            self.assertEqual(messages[-1]["content"], "assistant-2")
            db.close()

            code_list, payload_list = self._run_cli(["list", "--db", str(db_path)])
            self.assertEqual(code_list, 0)
            self.assertEqual(payload_list["count"], 1)
            self.assertEqual(payload_list["threads"][0]["thread_id"], "thread_send")
            self.assertEqual(payload_list["threads"][0]["latest_route"]["provider"], "provider-a")
            self.assertEqual(payload_list["threads"][0]["latest_route"]["account_id"], "acc-1")
            self.assertEqual(payload_list["threads"][0]["latest_route"]["model"], "gpt-5.4")
            self.assertTrue(payload_list["threads"][0]["send_available"])
            self.assertIsNone(payload_list["threads"][0]["send_unavailable_reason"])
            self.assertEqual(payload_list["threads"][0]["last_message_role"], "assistant")
            self.assertEqual(payload_list["threads"][0]["last_message_status"], "completed")
            self.assertEqual(payload_list["threads"][0]["last_message_preview"], "assistant-2")

    def test_send_command_returns_summary_ui_notice(self):
        class FakeProviderClient:
            def __init__(self, *, base_url: str, api_key: str, timeout_sec: float):
                self.base_url = base_url
                self.api_key = api_key
                self.timeout_sec = timeout_sec

            def stream(self, request):
                yield StreamEvent(kind="delta", text="answer")
                yield StreamEvent(kind="done", remote_response_id="resp")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "send_summary_notice.db"

            with patch("history_repair.cli.ResponsesApiProviderClient", FakeProviderClient):
                code1, _ = self._run_cli(
                    [
                        "send",
                        "--db",
                        str(db_path),
                        "--thread-id",
                        "thread_summary_notice",
                        "--message",
                        "A" * 120,
                        "--provider",
                        "provider-a",
                        "--account-id",
                        "acc-1",
                        "--model",
                        "gpt-5.4",
                        "--base-url",
                        "https://example.test",
                        "--api-key",
                        "test-key",
                        "--no-supports-previous-response-id",
                    ]
                )
                code2, payload2 = self._run_cli(
                    [
                        "send",
                        "--db",
                        str(db_path),
                        "--thread-id",
                        "thread_summary_notice",
                        "--message",
                        "B" * 120,
                        "--provider",
                        "provider-a",
                        "--account-id",
                        "acc-1",
                        "--model",
                        "gpt-5.4",
                        "--base-url",
                        "https://example.test",
                        "--api-key",
                        "test-key",
                        "--no-supports-previous-response-id",
                        "--max-context-tokens",
                        "80",
                    ]
                )

            self.assertEqual(code1, 0)
            self.assertEqual(code2, 0)
            self.assertEqual(payload2["continuation_mode"], "summary_rebuild")
            self.assertEqual(payload2["ui_notice"], "compressed earlier context to continue the thread")

    def test_send_command_uses_api_key_from_env(self):
        class FakeProviderClient:
            init_keys = []

            def __init__(self, *, base_url: str, api_key: str, timeout_sec: float):
                FakeProviderClient.init_keys.append(api_key)

            def stream(self, request):
                yield StreamEvent(kind="delta", text="ok")
                yield StreamEvent(kind="done", remote_response_id="resp_env")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "send_env.db"

            with patch("history_repair.cli.ResponsesApiProviderClient", FakeProviderClient):
                with patch.dict("os.environ", {"HISTORY_REPAIR_API_KEY": "env-secret"}, clear=True):
                    code, payload = self._run_cli(
                        [
                            "send",
                            "--db",
                            str(db_path),
                            "--thread-id",
                            "thread_env",
                            "--message",
                            "env key message",
                            "--provider",
                            "provider-a",
                            "--account-id",
                            "acc-1",
                            "--model",
                            "gpt-5.4",
                            "--base-url",
                            "https://example.test",
                        ]
                    )

            self.assertEqual(code, 0)
            self.assertEqual(payload["command"], "send")
            self.assertTrue(payload["success"])
            self.assertEqual(FakeProviderClient.init_keys, ["env-secret"])

    def test_send_command_missing_api_key_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "send_missing_key.db"

            with patch.dict("os.environ", {}, clear=True):
                code, payload = self._run_cli(
                    [
                        "send",
                        "--db",
                        str(db_path),
                        "--thread-id",
                        "thread_missing",
                        "--message",
                        "missing key",
                        "--provider",
                        "provider-a",
                        "--account-id",
                        "acc-1",
                        "--model",
                        "gpt-5.4",
                        "--base-url",
                        "https://example.test",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["command"], "send")
            self.assertIn("missing api key", payload["error"])

    def test_list_and_show_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "list_show.db"

            db = SessionDatabase(db_path)
            db.apply_migrations()
            thread_repo = ThreadRepository(db)
            message_repo = MessageRepository(db)
            thread_repo.create_thread(thread_id="thread_list", title="List Thread")
            message_repo.create_message(
                thread_id="thread_list",
                role="user",
                content="hello list",
                status=MessageStatus.COMPLETED,
            )
            db.close()

            code_list, payload_list = self._run_cli(["list", "--db", str(db_path)])
            self.assertEqual(code_list, 0)
            self.assertEqual(payload_list["command"], "list")
            self.assertEqual(payload_list["count"], 1)
            self.assertEqual(payload_list["threads"][0]["thread_id"], "thread_list")
            self.assertTrue(payload_list["threads"][0]["send_available"])
            self.assertIsNone(payload_list["threads"][0]["send_unavailable_reason"])
            self.assertEqual(payload_list["threads"][0]["last_message_role"], "user")
            self.assertEqual(payload_list["threads"][0]["last_message_status"], "completed")
            self.assertEqual(payload_list["threads"][0]["last_message_preview"], "hello list")

            code_show, payload_show = self._run_cli(
                ["show", "--db", str(db_path), "--thread-id", "thread_list"]
            )
            self.assertEqual(code_show, 0)
            self.assertEqual(payload_show["command"], "show")
            self.assertEqual(payload_show["thread"]["thread_id"], "thread_list")
            self.assertEqual(len(payload_show["messages"]), 1)
            self.assertEqual(payload_show["messages"][0]["content"], "hello list")

    def test_read_only_recovery_mode_still_allows_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported.db"
            self._create_unsupported_schema_db(db_path, with_thread=True)

            code, payload = self._run_cli(["list", "--db", str(db_path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["command"], "list")
            self.assertTrue(payload["read_only_mode"])
            self.assertIn("Unsupported schema_version", payload["migration_warning"])
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["threads"][0]["thread_id"], "thread_ro")
            self.assertFalse(payload["threads"][0]["send_available"])
            self.assertEqual(
                payload["threads"][0]["send_unavailable_reason"],
                "database is in read-only recovery mode",
            )
            self.assertTrue(Path(payload["migration_backup_path"]).exists())

    def test_read_only_recovery_mode_without_threads_table_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported_empty.db"
            self._create_unsupported_schema_db(db_path, with_thread=False)

            code, payload = self._run_cli(["list", "--db", str(db_path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["command"], "list")
            self.assertTrue(payload["read_only_mode"])
            self.assertEqual(payload["count"], 0)
            self.assertEqual(payload["threads"], [])

    def test_read_only_recovery_mode_blocks_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unsupported_send.db"
            self._create_unsupported_schema_db(db_path, with_thread=False)

            code, payload = self._run_cli(
                [
                    "send",
                    "--db",
                    str(db_path),
                    "--thread-id",
                    "thread_missing",
                    "--message",
                    "hello",
                    "--provider",
                    "provider-a",
                    "--account-id",
                    "acc-1",
                    "--model",
                    "gpt-5.4",
                    "--base-url",
                    "https://example.test",
                    "--api-key",
                    "test-key",
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["command"], "send")
            self.assertTrue(payload["read_only_mode"])
            self.assertIn("read-only recovery mode", payload["error"])


if __name__ == "__main__":
    unittest.main()
