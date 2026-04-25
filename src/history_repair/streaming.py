from __future__ import annotations

from .models import MessageStatus
from .repositories import MessageRepository


class StreamWriter:
    def __init__(self, message_repo: MessageRepository):
        self.message_repo = message_repo

    def append_chunk(self, message_id: str, chunk: str) -> None:
        self.message_repo.append_assistant_chunk(message_id, chunk)

    def complete(self, message_id: str) -> None:
        self.message_repo.set_status(message_id, MessageStatus.COMPLETED)

    def fail(self, message_id: str, *, error_code: str | None, error_message: str) -> None:
        current = self.message_repo.get_message(message_id)
        if not current:
            return
        if current.get("content"):
            self.message_repo.set_status(
                message_id,
                MessageStatus.PARTIAL,
                error_code=error_code,
                error_message=error_message,
            )
            return
        self.message_repo.set_status(
            message_id,
            MessageStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
        )

    def cancel(self, message_id: str, *, reason: str) -> None:
        self.message_repo.set_status(
            message_id,
            MessageStatus.CANCELED,
            error_code="CANCELED",
            error_message=reason,
        )
