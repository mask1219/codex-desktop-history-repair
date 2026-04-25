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
    MessageStatus,
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
from history_repair.service import ProviderStreamError  # noqa: E402


class RecordingProvider:
    def __init__(self, scenarios: list[dict]):
        self.scenarios = scenarios
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        scenario = self.scenarios.pop(0)
        for chunk in scenario.get("chunks", []):
            yield StreamEvent(kind="delta", text=chunk)
        raise_error = scenario.get("raise_error")
        if raise_error:
            if isinstance(raise_error, Exception):
                raise raise_error
            raise RuntimeError(str(raise_error))
        yield StreamEvent(kind="done", remote_response_id=scenario.get("remote_response_id"))


class InvalidPreviousIdProvider:
    def __init__(self):
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        call_index = len(self.requests)
        if call_index == 1:
            yield StreamEvent(kind="delta", text="seed")
            yield StreamEvent(kind="done", remote_response_id="resp_1")
            return
        if request.previous_response_id:
            yield StreamEvent(
                kind="error",
                error_code="invalid_previous_response_id",
                error_message="previous_response_id not found",
            )
            return
        yield StreamEvent(kind="delta", text="fallback answer")
        yield StreamEvent(kind="done", remote_response_id="resp_2")


class FailingSummaryManager:
    def build_summary_context(self, **kwargs):
        raise RuntimeError("summary build failed")


def build_service(db_path: Path) -> tuple[ChatSessionService, SessionDatabase, MessageRepository, RouteRepository, SummaryRepository]:
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
    return service, db, message_repo, route_repo, summary_repo


class Phase1Tests(unittest.TestCase):
    def test_schema_version_initialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "history.db"
            db = SessionDatabase(db_path)
            db.apply_migrations()
            version_row = db.query_one("SELECT value FROM app_meta WHERE key = 'schema_version'")
            self.assertIsNotNone(version_row)
            self.assertEqual(version_row["value"], "1")
            db.close()

    def test_remote_chain_reused_when_route_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, _, route_repo, _ = build_service(Path(tmp) / "history.db")
            provider = RecordingProvider(
                [
                    {"chunks": ["hello"], "remote_response_id": "resp_1"},
                    {"chunks": ["follow up"], "remote_response_id": "resp_2"},
                ]
            )
            target = RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4")
            caps = ProviderCapabilities(supports_previous_response_id=True, max_context_tokens=5000)

            service.send_message(
                thread_id="thread_1",
                user_content="第一条消息",
                route_target=target,
                capabilities=caps,
                provider_client=provider,
            )
            result = service.send_message(
                thread_id="thread_1",
                user_content="第二条消息",
                route_target=target,
                capabilities=caps,
                provider_client=provider,
            )

            latest_route = route_repo.get_latest_route("thread_1")
            self.assertEqual(result.continuation_mode, ContinuationMode.REMOTE_CHAIN)
            self.assertEqual(latest_route["continuation_mode"], ContinuationMode.REMOTE_CHAIN.value)
            self.assertEqual(provider.requests[1].previous_response_id, "resp_1")
            self.assertEqual(provider.requests[1].messages[0]["role"], "user")
            self.assertEqual(provider.requests[1].messages[0]["content"], "第二条消息")
            db.close()

    def test_switch_provider_falls_back_to_local_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, _, route_repo, _ = build_service(Path(tmp) / "history.db")
            provider = RecordingProvider(
                [
                    {"chunks": ["hi"], "remote_response_id": "resp_1"},
                    {"chunks": ["ok"], "remote_response_id": "resp_2"},
                ]
            )
            caps = ProviderCapabilities(supports_previous_response_id=True, max_context_tokens=5000)

            service.send_message(
                thread_id="thread_2",
                user_content="message one",
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=caps,
                provider_client=provider,
            )
            service.send_message(
                thread_id="thread_2",
                user_content="message two",
                route_target=RouteTarget(provider="provider-b", account_id="acc-1", model="gpt-5.4"),
                capabilities=caps,
                provider_client=provider,
            )

            latest_route = route_repo.get_latest_route("thread_2")
            self.assertEqual(latest_route["continuation_mode"], ContinuationMode.LOCAL_REBUILD.value)
            self.assertIsNone(provider.requests[1].previous_response_id)
            self.assertGreaterEqual(len(provider.requests[1].messages), 3)
            db.close()

    def test_summary_rebuild_when_context_exceeds_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, _, route_repo, summary_repo = build_service(Path(tmp) / "history.db")
            provider = RecordingProvider(
                [
                    {"chunks": ["answer one"], "remote_response_id": "resp_1"},
                    {"chunks": ["answer two"], "remote_response_id": "resp_2"},
                ]
            )
            first_caps = ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5000)
            second_caps = ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=80)
            target = RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4")

            service.send_message(
                thread_id="thread_3",
                user_content="A" * 120,
                route_target=target,
                capabilities=first_caps,
                provider_client=provider,
            )
            result = service.send_message(
                thread_id="thread_3",
                user_content="B" * 120,
                route_target=target,
                capabilities=second_caps,
                provider_client=provider,
            )

            latest_route = route_repo.get_latest_route("thread_3")
            summaries = summary_repo.list_summaries("thread_3")
            self.assertEqual(result.continuation_mode, ContinuationMode.SUMMARY_REBUILD)
            self.assertEqual(latest_route["continuation_mode"], ContinuationMode.SUMMARY_REBUILD.value)
            self.assertGreaterEqual(len(summaries), 1)
            self.assertEqual(provider.requests[1].messages[0]["role"], "system")
            db.close()

    def test_stream_failure_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, message_repo, _, _ = build_service(Path(tmp) / "history.db")
            provider = RecordingProvider(
                [
                    {"chunks": ["partial-output"], "raise_error": "network disconnected"},
                ]
            )
            result = service.send_message(
                thread_id="thread_4",
                user_content="trigger fail",
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5000),
                provider_client=provider,
            )

            assistant = message_repo.get_message(result.assistant_message_id)
            self.assertFalse(result.success)
            self.assertEqual(assistant["status"], "partial")
            self.assertEqual(assistant["content"], "partial-output")
            db.close()

    def test_failed_request_persists_error_code_and_thread_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, message_repo, _, _ = build_service(Path(tmp) / "history.db")
            thread_repo = ThreadRepository(db)
            provider = RecordingProvider(
                [
                    {
                        "chunks": [],
                        "raise_error": ProviderStreamError("rate limited", error_code="RATE_LIMIT"),
                    },
                ]
            )
            result = service.send_message(
                thread_id="thread_error_code",
                user_content="trigger error",
                route_target=RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4"),
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5000),
                provider_client=provider,
            )

            assistant = message_repo.get_message(result.assistant_message_id)
            thread = thread_repo.get_thread("thread_error_code")
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "RATE_LIMIT")
            self.assertEqual(assistant["status"], "failed")
            self.assertEqual(assistant["error_code"], "RATE_LIMIT")
            self.assertEqual(thread["last_continuation_error"], "[RATE_LIMIT] rate limited")
            db.close()

    def test_startup_recovery_converges_streaming_and_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, message_repo, _, _ = build_service(Path(tmp) / "history.db")
            thread_repo = ThreadRepository(db)
            thread_repo.create_thread(thread_id="thread_5", title="t5")

            old_ts = 1_000
            pending = message_repo.create_message(
                thread_id="thread_5",
                role="assistant",
                content="",
                status=MessageStatus.PENDING,
                created_at_ms=old_ts,
            )
            streaming = message_repo.create_message(
                thread_id="thread_5",
                role="assistant",
                content="streamed",
                status=MessageStatus.STREAMING,
                created_at_ms=old_ts,
            )

            message_repo.recover_incomplete_assistant_messages(now_ms_value=100_000, pending_timeout_ms=5_000)
            pending_after = message_repo.get_message(pending["message_id"])
            streaming_after = message_repo.get_message(streaming["message_id"])

            self.assertEqual(pending_after["status"], "failed")
            self.assertEqual(streaming_after["status"], "partial")
            db.close()

    def test_remote_chain_checkpoint_error_retries_with_local_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, message_repo, route_repo, _ = build_service(Path(tmp) / "history.db")
            provider = InvalidPreviousIdProvider()
            target = RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4")
            caps = ProviderCapabilities(supports_previous_response_id=True, max_context_tokens=5000)

            service.send_message(
                thread_id="thread_retry",
                user_content="first",
                route_target=target,
                capabilities=caps,
                provider_client=provider,
            )
            result = service.send_message(
                thread_id="thread_retry",
                user_content="second",
                route_target=target,
                capabilities=caps,
                provider_client=provider,
            )

            latest_route = route_repo.get_latest_route("thread_retry")
            assistant = message_repo.get_message(result.assistant_message_id)
            self.assertTrue(result.success)
            self.assertEqual(result.continuation_mode, ContinuationMode.LOCAL_REBUILD)
            self.assertIn("previous_response_id", result.continuation_note or "")
            self.assertEqual(assistant["status"], "completed")
            self.assertEqual(assistant["content"], "fallback answer")
            self.assertEqual(len(provider.requests), 3)
            self.assertEqual(provider.requests[1].previous_response_id, "resp_1")
            self.assertIsNone(provider.requests[2].previous_response_id)
            self.assertGreaterEqual(len(provider.requests[2].messages), 3)
            self.assertEqual(latest_route["continuation_mode"], ContinuationMode.LOCAL_REBUILD.value)
            self.assertIsNone(latest_route["previous_response_id"])
            self.assertEqual(latest_route["remote_response_id"], "resp_2")
            db.close()

    def test_summary_generation_failure_falls_back_to_recent_local_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDatabase(Path(tmp) / "history.db")
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
                summary_manager=FailingSummaryManager(),
                stream_writer=StreamWriter(message_repo),
            )

            provider = RecordingProvider(
                [
                    {"chunks": ["first answer"], "remote_response_id": "resp_1"},
                    {"chunks": ["second answer"], "remote_response_id": "resp_2"},
                ]
            )
            target = RouteTarget(provider="provider-a", account_id="acc-1", model="gpt-5.4")

            service.send_message(
                thread_id="thread_summary_fallback",
                user_content="A" * 120,
                route_target=target,
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=5000),
                provider_client=provider,
            )
            result = service.send_message(
                thread_id="thread_summary_fallback",
                user_content="B" * 120,
                route_target=target,
                capabilities=ProviderCapabilities(supports_previous_response_id=False, max_context_tokens=80),
                provider_client=provider,
            )

            latest_route = route_repo.get_latest_route("thread_summary_fallback")
            self.assertTrue(result.success)
            self.assertEqual(result.continuation_mode, ContinuationMode.LOCAL_REBUILD)
            self.assertIn("summary generation failed", result.continuation_note or "")
            self.assertIsNone(result.summary_id)
            self.assertEqual(latest_route["continuation_mode"], ContinuationMode.LOCAL_REBUILD.value)
            self.assertEqual(len(summary_repo.list_summaries("thread_summary_fallback")), 0)
            self.assertNotEqual(provider.requests[1].messages[0]["role"], "system")
            db.close()


if __name__ == "__main__":
    unittest.main()
