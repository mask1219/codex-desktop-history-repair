from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import (  # noqa: E402
    ChatSessionService,
    ContinuationMode,
    ContinuationPlanner,
    ExportImportService,
    MessageRepository,
    ProviderCapabilities,
    RouteRepository,
    RouteTarget,
    SessionDatabase,
    StreamEvent,
    StreamWriter,
    SummaryManager,
    SummaryRepository,
    ThreadRepository,
)


class StaticProvider:
    def __init__(self, chunks: list[str], remote_response_id: str):
        self.chunks = chunks
        self.remote_response_id = remote_response_id
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        for chunk in self.chunks:
            yield StreamEvent(kind="delta", text=chunk)
        yield StreamEvent(kind="done", remote_response_id=self.remote_response_id)


def build_service(db_path: Path) -> tuple[ChatSessionService, SessionDatabase, MessageRepository, RouteRepository, ThreadRepository]:
    db = SessionDatabase(db_path)
    db.apply_migrations()
    thread_repo = ThreadRepository(db)
    message_repo = MessageRepository(db)
    route_repo = RouteRepository(db)
    summary_repo = SummaryRepository(db)
    service = ChatSessionService(
        thread_repo=thread_repo,
        message_repo=message_repo,
        route_repo=route_repo,
        planner=ContinuationPlanner(),
        summary_manager=SummaryManager(summary_repo),
        stream_writer=StreamWriter(message_repo),
    )
    return service, db, message_repo, route_repo, thread_repo


class ContinueAfterImportTests(unittest.TestCase):
    def test_imported_thread_can_continue_with_local_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_service, source_db, _, _, _ = build_service(tmp_path / "source.db")
            source_provider = StaticProvider(chunks=["source-assistant"], remote_response_id="resp_source")

            source_service.send_message(
                thread_id="thread_e2e",
                user_content="seed message",
                route_target=RouteTarget(provider="provider-a", account_id="acc-a", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5000),
                provider_client=source_provider,
            )

            export_file = tmp_path / "export.jsonl"
            ExportImportService(source_db).export_jsonl(export_file)

            target_service, target_db, target_messages, _, target_threads = build_service(tmp_path / "target.db")
            report = ExportImportService(target_db).import_jsonl(export_file)
            imported_thread_id = report.thread_id_map["thread_e2e"]

            continue_provider = StaticProvider(chunks=["continued-answer"], remote_response_id="resp_continued")
            result = target_service.send_message(
                thread_id=imported_thread_id,
                user_content="continue this thread",
                route_target=RouteTarget(provider="provider-b", account_id="acc-b", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=True, max_context_tokens=5000),
                provider_client=continue_provider,
            )

            all_messages = target_messages.list_messages(imported_thread_id)
            self.assertTrue(result.success)
            self.assertEqual(result.thread_id, imported_thread_id)
            self.assertEqual(result.continuation_mode, ContinuationMode.LOCAL_REBUILD)
            self.assertEqual(len(all_messages), 4)
            self.assertEqual(all_messages[-1]["role"], "assistant")
            self.assertEqual(all_messages[-1]["content"], "continued-answer")
            self.assertIsNotNone(target_threads.get_thread(imported_thread_id))

            source_db.close()
            target_db.close()


if __name__ == "__main__":
    unittest.main()
