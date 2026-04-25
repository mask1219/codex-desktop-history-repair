from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import (  # noqa: E402
    ExportImportService,
    MessageRepository,
    MessageStatus,
    RouteRepository,
    SessionDatabase,
    SummaryRepository,
    ThreadRepository,
)


class ExportImportTests(unittest.TestCase):
    def test_export_import_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_db = SessionDatabase(tmp_path / "source.db")
            source_db.apply_migrations()
            thread_repo = ThreadRepository(source_db)
            message_repo = MessageRepository(source_db)
            route_repo = RouteRepository(source_db)
            summary_repo = SummaryRepository(source_db)

            thread_repo.create_thread(thread_id="thread_roundtrip", title="Roundtrip")
            message_repo.create_message(
                thread_id="thread_roundtrip",
                role="user",
                content="hello",
                status=MessageStatus.COMPLETED,
            )
            message_repo.create_message(
                thread_id="thread_roundtrip",
                role="assistant",
                content="hi",
                status=MessageStatus.COMPLETED,
            )
            route_repo.create_route(
                thread_id="thread_roundtrip",
                provider="provider-a",
                account_id="acc-1",
                model="gpt-5.4",
                continuation_mode="local_rebuild",
                previous_response_id=None,
                remote_response_id="resp_abc",
                capabilities_snapshot='{"supports_previous_response_id":true}',
            )
            summary_repo.create_summary(
                thread_id="thread_roundtrip",
                source_start_seq=1,
                source_end_seq=2,
                summary_text="summary text",
                status="completed",
                model="gpt-5.4",
            )

            export_file = tmp_path / "history_export.jsonl"
            export_service = ExportImportService(source_db)
            line_count = export_service.export_jsonl(export_file)
            self.assertEqual(line_count, 5)

            target_db = SessionDatabase(tmp_path / "target.db")
            target_db.apply_migrations()
            import_service = ExportImportService(target_db)
            report = import_service.import_jsonl(export_file)

            target_thread_repo = ThreadRepository(target_db)
            target_message_repo = MessageRepository(target_db)
            target_route_repo = RouteRepository(target_db)
            target_summary_repo = SummaryRepository(target_db)

            self.assertEqual(report.imported_threads, 1)
            self.assertEqual(report.imported_messages, 2)
            self.assertEqual(report.imported_routes, 1)
            self.assertEqual(report.imported_summaries, 1)
            self.assertEqual(report.thread_id_map["thread_roundtrip"], "thread_roundtrip")
            self.assertEqual(target_thread_repo.get_thread("thread_roundtrip")["title"], "Roundtrip")
            self.assertEqual(len(target_message_repo.list_messages("thread_roundtrip")), 2)
            self.assertEqual(len(target_route_repo.list_routes("thread_roundtrip")), 1)
            self.assertEqual(len(target_summary_repo.list_summaries("thread_roundtrip")), 1)

            source_db.close()
            target_db.close()

    def test_import_conflict_uses_thread_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = SessionDatabase(tmp_path / "conflict.db")
            db.apply_migrations()
            thread_repo = ThreadRepository(db)
            thread_repo.create_thread(thread_id="thread_conflict", title="Original")

            import_file = tmp_path / "conflict_import.jsonl"
            items = [
                {
                    "type": "thread",
                    "data": {
                        "thread_id": "thread_conflict",
                        "title": "Imported Copy",
                        "status": "active",
                        "created_at": 1000,
                        "updated_at": 1000,
                        "last_message_at": 1000,
                        "last_continuation_error": None,
                    },
                },
                {
                    "type": "message",
                    "data": {
                        "message_id": "msg_import_1",
                        "thread_id": "thread_conflict",
                        "seq": 1,
                        "role": "user",
                        "content": "imported text",
                        "status": "completed",
                        "created_at": 1000,
                        "updated_at": 1000,
                        "provider": None,
                        "account_id": None,
                        "model": None,
                        "request_id": None,
                        "error_code": None,
                        "error_message": None,
                    },
                },
            ]
            with import_file.open("w", encoding="utf-8") as f:
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")

            report = ExportImportService(db).import_jsonl(import_file)
            new_thread_id = report.thread_id_map["thread_conflict"]

            self.assertNotEqual(new_thread_id, "thread_conflict")
            self.assertTrue(new_thread_id.startswith("thread_conflict_import_"))
            self.assertEqual(thread_repo.get_thread("thread_conflict")["title"], "Original")
            self.assertEqual(thread_repo.get_thread(new_thread_id)["title"], "Imported Copy")

            message_repo = MessageRepository(db)
            imported_messages = message_repo.list_messages(new_thread_id)
            self.assertEqual(len(imported_messages), 1)
            self.assertEqual(imported_messages[0]["content"], "imported text")
            db.close()

    def test_import_validation_missing_required_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = SessionDatabase(tmp_path / "validation.db")
            db.apply_migrations()
            bad_file = tmp_path / "bad.jsonl"
            with bad_file.open("w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "thread",
                            "data": {
                                "thread_id": "missing_title",
                                "status": "active",
                                "created_at": 1,
                                "updated_at": 1,
                                "last_message_at": 1,
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            with self.assertRaises(ValueError):
                ExportImportService(db).import_jsonl(bad_file)
            db.close()


if __name__ == "__main__":
    unittest.main()
