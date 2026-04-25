from __future__ import annotations

from typing import Any

from .repositories import SummaryRepository


class SummaryManager:
    def __init__(self, summary_repo: SummaryRepository):
        self.summary_repo = summary_repo

    def build_summary_context(
        self,
        *,
        thread_id: str,
        transcript_messages: list[dict[str, Any]],
        max_context_tokens: int,
        model: str,
    ) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
        if not transcript_messages:
            return [], None

        recent_budget = int(max_context_tokens * 0.55)
        if recent_budget <= 0:
            recent_budget = 1

        recent_messages: list[dict[str, Any]] = []
        running = 0
        for message in reversed(transcript_messages):
            cost = max(1, len(message.get("content", "")))
            if running + cost > recent_budget and recent_messages:
                break
            recent_messages.append(message)
            running += cost
        recent_messages.reverse()

        if len(recent_messages) >= len(transcript_messages):
            return [self._to_provider_message(msg) for msg in transcript_messages], None

        summary_source = transcript_messages[: len(transcript_messages) - len(recent_messages)]
        summary_text = self._summarize(summary_source)
        summary_record = self.summary_repo.create_summary(
            thread_id=thread_id,
            source_start_seq=summary_source[0]["seq"],
            source_end_seq=summary_source[-1]["seq"],
            summary_text=summary_text,
            status="completed",
            model=model,
        )

        provider_messages = [
            {
                "role": "system",
                "content": (
                    "Conversation summary (generated locally, may omit details):\n"
                    f"{summary_text}"
                ),
            }
        ]
        provider_messages.extend(self._to_provider_message(msg) for msg in recent_messages)
        return provider_messages, summary_record

    def _to_provider_message(self, message: dict[str, Any]) -> dict[str, str]:
        return {
            "role": str(message["role"]),
            "content": str(message["content"]),
        }

    def _summarize(self, messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = str(message["role"])
            content = str(message["content"]).strip().replace("\n", " ")
            if len(content) > 200:
                content = f"{content[:200]}..."
            lines.append(f"- {role}: {content}")
        if not lines:
            return "(no prior content)"
        return "\n".join(lines)
