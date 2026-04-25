from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol


class MessageStatus(str, Enum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"


class ContinuationMode(str, Enum):
    REMOTE_CHAIN = "remote_chain"
    LOCAL_REBUILD = "local_rebuild"
    SUMMARY_REBUILD = "summary_rebuild"


@dataclass(frozen=True)
class RouteTarget:
    provider: str
    account_id: str
    model: str


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_previous_response_id: bool = True
    max_context_tokens: int = 120000


@dataclass(frozen=True)
class PlannerDecision:
    mode: ContinuationMode
    reason: str
    previous_response_id: str | None = None


@dataclass(frozen=True)
class ProviderRequest:
    thread_id: str
    model: str
    continuation_mode: ContinuationMode
    messages: list[dict[str, str]]
    previous_response_id: str | None = None
    instructions: str | None = None


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    text: str = ""
    remote_response_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ProviderClient(Protocol):
    def stream(self, request: ProviderRequest) -> Iterable[StreamEvent]:
        ...
