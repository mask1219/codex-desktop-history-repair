from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .models import (
    ContinuationMode,
    MessageStatus,
    PlannerDecision,
    ProviderCapabilities,
    ProviderClient,
    ProviderRequest,
    RouteTarget,
)
from .planner import ContinuationPlanner
from .repositories import MessageRepository, RouteRepository, ThreadRepository
from .streaming import StreamWriter
from .summary import SummaryManager


@dataclass(frozen=True)
class SendResult:
    success: bool
    thread_id: str
    user_message_id: str
    assistant_message_id: str
    continuation_mode: ContinuationMode
    remote_response_id: str | None
    summary_id: str | None
    error: str | None
    error_code: str | None = None
    continuation_note: str | None = None


class ProviderStreamError(RuntimeError):
    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.error_message = message


class ChatSessionService:
    def __init__(
        self,
        *,
        thread_repo: ThreadRepository,
        message_repo: MessageRepository,
        route_repo: RouteRepository,
        planner: ContinuationPlanner,
        summary_manager: SummaryManager,
        stream_writer: StreamWriter,
    ):
        self.thread_repo = thread_repo
        self.message_repo = message_repo
        self.route_repo = route_repo
        self.planner = planner
        self.summary_manager = summary_manager
        self.stream_writer = stream_writer

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
    ) -> SendResult:
        self._ensure_thread(thread_id=thread_id, title=thread_title or self._derive_title(user_content))
        user_message = self.message_repo.create_message(
            thread_id=thread_id,
            role="user",
            content=user_content,
            status=MessageStatus.COMPLETED,
            provider=route_target.provider,
            account_id=route_target.account_id,
            model=route_target.model,
        )
        assistant_message = self.message_repo.create_message(
            thread_id=thread_id,
            role="assistant",
            content="",
            status=MessageStatus.PENDING,
            provider=route_target.provider,
            account_id=route_target.account_id,
            model=route_target.model,
        )

        latest_route = self.route_repo.get_latest_route(thread_id)
        last_remote_chain_failed = self._last_remote_chain_failed(thread_id, latest_route)
        transcript_messages = self.message_repo.list_context_messages(thread_id)
        decision = self.planner.select_mode(
            transcript_messages=transcript_messages,
            latest_route=latest_route,
            route_target=route_target,
            capabilities=capabilities,
            last_remote_chain_failed=last_remote_chain_failed,
        )
        request_messages, summary_record, request_mode, continuation_note = self._build_request_messages(
            decision=decision,
            transcript_messages=transcript_messages,
            max_context_tokens=capabilities.max_context_tokens,
            model=route_target.model,
            thread_id=thread_id,
        )

        remote_response_id: str | None = None
        error: str | None = None
        error_code: str | None = None
        success = False
        active_decision = decision
        active_messages = request_messages
        active_summary = summary_record
        active_mode = request_mode

        while True:
            request = ProviderRequest(
                thread_id=thread_id,
                model=route_target.model,
                continuation_mode=active_mode,
                messages=active_messages,
                previous_response_id=active_decision.previous_response_id,
                instructions=instructions,
            )
            try:
                remote_response_id = self._stream_provider(
                    provider_client=provider_client,
                    request=request,
                    assistant_message_id=assistant_message["message_id"],
                )
                self.stream_writer.complete(assistant_message["message_id"])
                self.thread_repo.set_last_continuation_error(thread_id, None)
                success = True
                break
            except ProviderStreamError as exc:
                retry_payload = self._prepare_retry_after_remote_chain_failure(
                    decision=active_decision,
                    error_code=exc.error_code,
                    error_message=exc.error_message,
                    assistant_message_id=assistant_message["message_id"],
                    transcript_messages=transcript_messages,
                    latest_route=latest_route,
                    route_target=route_target,
                    capabilities=capabilities,
                    max_context_tokens=capabilities.max_context_tokens,
                    model=route_target.model,
                    thread_id=thread_id,
                )
                if retry_payload is not None:
                    (
                        active_decision,
                        active_messages,
                        active_summary,
                        active_mode,
                        retry_note,
                    ) = retry_payload
                    continuation_note = self._merge_notes(continuation_note, retry_note)
                    continue
                error = exc.error_message
                error_code = exc.error_code
                break
            except Exception as exc:
                error = str(exc)
                break

        if not success:
            self.stream_writer.fail(
                assistant_message["message_id"],
                error_code=error_code,
                error_message=error or "provider stream error",
            )
            self.thread_repo.set_last_continuation_error(
                thread_id,
                self._format_last_continuation_error(error_code=error_code, error_message=error),
            )

        self.route_repo.create_route(
            thread_id=thread_id,
            provider=route_target.provider,
            account_id=route_target.account_id,
            model=route_target.model,
            continuation_mode=active_mode.value,
            previous_response_id=active_decision.previous_response_id,
            remote_response_id=remote_response_id,
            capabilities_snapshot=json.dumps(asdict(capabilities)),
        )
        self.thread_repo.touch_thread(thread_id)

        return SendResult(
            success=success,
            thread_id=thread_id,
            user_message_id=user_message["message_id"],
            assistant_message_id=assistant_message["message_id"],
            continuation_mode=active_mode,
            remote_response_id=remote_response_id,
            summary_id=active_summary["summary_id"] if active_summary else None,
            error=error,
            error_code=error_code,
            continuation_note=continuation_note,
        )

    def recover_incomplete_messages(self, *, pending_timeout_ms: int = 60_000) -> None:
        self.message_repo.recover_incomplete_assistant_messages(pending_timeout_ms=pending_timeout_ms)

    def _ensure_thread(self, *, thread_id: str, title: str) -> None:
        existing = self.thread_repo.get_thread(thread_id)
        if existing:
            return
        self.thread_repo.create_thread(thread_id=thread_id, title=title)

    def _derive_title(self, user_content: str) -> str:
        title = user_content.strip()
        if not title:
            return "New Thread"
        return title[:40]

    def _last_remote_chain_failed(self, thread_id: str, latest_route: dict[str, Any] | None) -> bool:
        if latest_route is None:
            return False
        if latest_route.get("continuation_mode") != ContinuationMode.REMOTE_CHAIN.value:
            return False
        if latest_route.get("remote_response_id"):
            return False
        messages = self.message_repo.list_messages(thread_id)
        last_assistant = None
        for message in reversed(messages):
            if message["role"] != "assistant":
                continue
            if message["status"] == MessageStatus.PENDING.value and not message["content"]:
                continue
            last_assistant = message
            break
        if not last_assistant:
            return False
        return last_assistant["status"] in {
            MessageStatus.FAILED.value,
            MessageStatus.PARTIAL.value,
            MessageStatus.CANCELED.value,
        }

    def _build_request_messages(
        self,
        *,
        decision: PlannerDecision,
        transcript_messages: list[dict[str, Any]],
        max_context_tokens: int,
        model: str,
        thread_id: str,
    ) -> tuple[list[dict[str, str]], dict[str, Any] | None, ContinuationMode, str | None]:
        if decision.mode == ContinuationMode.REMOTE_CHAIN:
            latest_user = transcript_messages[-1]
            return (
                [{"role": latest_user["role"], "content": latest_user["content"]}],
                None,
                ContinuationMode.REMOTE_CHAIN,
                None,
            )
        if decision.mode == ContinuationMode.LOCAL_REBUILD:
            return (
                [
                    {"role": message["role"], "content": message["content"]}
                    for message in transcript_messages
                ],
                None,
                ContinuationMode.LOCAL_REBUILD,
                None,
            )
        try:
            provider_messages, summary_record = self.summary_manager.build_summary_context(
                thread_id=thread_id,
                transcript_messages=transcript_messages,
                max_context_tokens=max_context_tokens,
                model=model,
            )
            return (
                provider_messages,
                summary_record,
                ContinuationMode.SUMMARY_REBUILD,
                "compressed earlier context to continue the thread",
            )
        except Exception:
            fallback_messages = self._build_recent_window_messages(
                transcript_messages=transcript_messages,
                max_context_tokens=max_context_tokens,
            )
            note = "summary generation failed; fallback to recent local context window"
            return fallback_messages, None, ContinuationMode.LOCAL_REBUILD, note

    def _stream_provider(
        self,
        *,
        provider_client: ProviderClient,
        request: ProviderRequest,
        assistant_message_id: str,
    ) -> str | None:
        remote_response_id: str | None = None
        for event in provider_client.stream(request):
            if event.kind == "delta":
                self.stream_writer.append_chunk(assistant_message_id, event.text)
                continue
            if event.kind == "done":
                remote_response_id = event.remote_response_id
                continue
            if event.kind == "error":
                raise ProviderStreamError(
                    event.error_message or "provider stream error",
                    error_code=event.error_code,
                )
        return remote_response_id

    def _prepare_retry_after_remote_chain_failure(
        self,
        *,
        decision: PlannerDecision,
        error_code: str | None,
        error_message: str,
        assistant_message_id: str,
        transcript_messages: list[dict[str, Any]],
        latest_route: dict[str, Any] | None,
        route_target: RouteTarget,
        capabilities: ProviderCapabilities,
        max_context_tokens: int,
        model: str,
        thread_id: str,
    ) -> tuple[PlannerDecision, list[dict[str, str]], dict[str, Any] | None, ContinuationMode, str] | None:
        if decision.mode != ContinuationMode.REMOTE_CHAIN:
            return None
        if not self._looks_like_checkpoint_error(error_code=error_code, error_message=error_message):
            return None
        assistant_message = self.message_repo.get_message(assistant_message_id)
        if assistant_message and assistant_message.get("content"):
            return None

        fallback_decision = self.planner.select_mode(
            transcript_messages=transcript_messages,
            latest_route=latest_route,
            route_target=route_target,
            capabilities=capabilities,
            last_remote_chain_failed=True,
        )
        request_messages, summary_record, request_mode, summary_note = self._build_request_messages(
            decision=fallback_decision,
            transcript_messages=transcript_messages,
            max_context_tokens=max_context_tokens,
            model=model,
            thread_id=thread_id,
        )
        note = "previous_response_id reuse failed; retried with local transcript context"
        if summary_note:
            note = self._merge_notes(note, summary_note)
        return fallback_decision, request_messages, summary_record, request_mode, note

    def _looks_like_checkpoint_error(self, *, error_code: str | None, error_message: str) -> bool:
        normalized_code = (error_code or "").strip().lower()
        if normalized_code in {
            "invalid_previous_response_id",
            "previous_response_id_invalid",
            "response_not_found",
            "checkpoint_not_found",
        }:
            return True

        text = error_message.strip().lower()
        if not text:
            return False
        if "previous_response_id" in text:
            return True
        if "previous response id" in text:
            return True
        if "checkpoint" in text and ("invalid" in text or "not found" in text):
            return True
        if "response" in text and "not found" in text and "previous" in text:
            return True
        return False

    def _build_recent_window_messages(
        self,
        *,
        transcript_messages: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> list[dict[str, str]]:
        budget = max(1, int(max_context_tokens))
        selected: list[dict[str, str]] = []
        running = 0
        for message in reversed(transcript_messages):
            role = str(message["role"])
            content = str(message["content"])
            cost = max(1, len(content))
            if running + cost > budget and selected:
                break
            selected.append({"role": role, "content": content})
            running += cost
        selected.reverse()
        return selected

    def _merge_notes(self, existing: str | None, new_note: str | None) -> str | None:
        if not new_note:
            return existing
        if not existing:
            return new_note
        return f"{existing}; {new_note}"

    def _format_last_continuation_error(
        self,
        *,
        error_code: str | None,
        error_message: str | None,
    ) -> str:
        message = (error_message or "unknown provider error").strip()
        if error_code:
            return f"[{error_code}] {message}"
        return message
