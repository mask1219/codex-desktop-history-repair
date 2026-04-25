from __future__ import annotations

import json
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest

from .models import ProviderRequest, StreamEvent


class ResponsesApiProviderClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_sec: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.extra_headers = extra_headers or {}

    def stream(self, request: ProviderRequest) -> Iterable[StreamEvent]:
        payload: dict[str, Any] = {
            "model": request.model,
            "input": [
                {
                    "role": message["role"],
                    "content": message["content"],
                }
                for message in request.messages
            ],
            "stream": True,
        }
        if request.previous_response_id:
            payload["previous_response_id"] = request.previous_response_id
        if request.instructions:
            payload["instructions"] = request.instructions

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream,application/json",
            **self.extra_headers,
        }

        req = urlrequest.Request(
            url=self._responses_url(),
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urlrequest.urlopen(req, timeout=self.timeout_sec) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    yield from self._stream_sse(resp)
                    return
                response_text = resp.read().decode("utf-8")
                yield from self._stream_json_response(response_text)
        except urlerror.HTTPError as exc:
            error_message, error_code = self._parse_http_error(exc)
            yield StreamEvent(kind="error", error_code=error_code, error_message=error_message)
        except urlerror.URLError as exc:
            yield StreamEvent(kind="error", error_code="NETWORK_ERROR", error_message=str(exc.reason))
        except Exception as exc:
            yield StreamEvent(kind="error", error_code="PROVIDER_CLIENT_ERROR", error_message=str(exc))

    def _responses_url(self) -> str:
        if self.base_url.endswith("/responses"):
            return self.base_url
        return f"{self.base_url}/responses"

    def _stream_json_response(self, response_text: str) -> Iterable[StreamEvent]:
        payload = json.loads(response_text)
        if "error" in payload:
            error_data = payload["error"] or {}
            error_message = str(error_data.get("message", "provider returned error"))
            error_code = str(error_data.get("code", "PROVIDER_ERROR"))
            yield StreamEvent(kind="error", error_code=error_code, error_message=error_message)
            return

        output_text = self._extract_output_text(payload)
        if output_text:
            yield StreamEvent(kind="delta", text=output_text)
        remote_response_id = self._extract_response_id(payload)
        yield StreamEvent(kind="done", remote_response_id=remote_response_id)

    def _stream_sse(self, resp: Any) -> Iterable[StreamEvent]:
        data_lines: list[str] = []
        remote_response_id: str | None = None
        emitted_done = False

        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line:
                if not data_lines:
                    continue
                for event in self._events_from_sse_data("\n".join(data_lines)):
                    if event.kind == "done":
                        if event.remote_response_id:
                            remote_response_id = event.remote_response_id
                        if not emitted_done:
                            yield event
                            emitted_done = True
                    elif event.kind == "delta":
                        yield event
                    elif event.kind == "error":
                        yield event
                data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            for event in self._events_from_sse_data("\n".join(data_lines)):
                if event.kind == "done":
                    if event.remote_response_id:
                        remote_response_id = event.remote_response_id
                    if not emitted_done:
                        yield event
                        emitted_done = True
                elif event.kind == "delta":
                    yield event
                elif event.kind == "error":
                    yield event

        if not emitted_done:
            yield StreamEvent(kind="done", remote_response_id=remote_response_id)

    def _events_from_sse_data(self, data: str) -> list[StreamEvent]:
        if data == "[DONE]":
            return [StreamEvent(kind="done")]

        payload = json.loads(data)
        event_type = payload.get("type")

        if event_type in {"response.output_text.delta", "output_text.delta"}:
            delta = str(payload.get("delta", ""))
            if not delta:
                return []
            return [StreamEvent(kind="delta", text=delta)]

        if event_type in {"response.error", "error"} or "error" in payload:
            error_data = payload.get("error", payload)
            error_message = str(error_data.get("message", "provider stream error"))
            error_code = str(error_data.get("code", "PROVIDER_STREAM_ERROR"))
            return [StreamEvent(kind="error", error_code=error_code, error_message=error_message)]

        if event_type in {"response.completed", "response.done", "completed"}:
            response_id = self._extract_response_id(payload)
            return [StreamEvent(kind="done", remote_response_id=response_id)]

        if "delta" in payload and isinstance(payload.get("delta"), str):
            return [StreamEvent(kind="delta", text=str(payload["delta"]))]

        return []

    def _extract_response_id(self, payload: dict[str, Any]) -> str | None:
        if payload.get("response") and isinstance(payload["response"], dict):
            response_id = payload["response"].get("id")
            if response_id:
                return str(response_id)
        if payload.get("id"):
            return str(payload["id"])
        return None

    def _extract_output_text(self, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return str(payload["output_text"])

        outputs = payload.get("output")
        if not isinstance(outputs, list):
            return ""

        parts: list[str] = []
        for output in outputs:
            if not isinstance(output, dict):
                continue
            content = output.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
                    parts.append(str(item["text"]))
        return "".join(parts)

    def _parse_http_error(self, exc: urlerror.HTTPError) -> tuple[str, str]:
        code = str(exc.code)
        message = f"HTTP {exc.code}"
        try:
            body = exc.read().decode("utf-8")
            payload = json.loads(body)
            if isinstance(payload, dict):
                error_data = payload.get("error")
                if isinstance(error_data, dict):
                    if isinstance(error_data.get("message"), str):
                        message = error_data["message"]
                    if isinstance(error_data.get("code"), str):
                        code = error_data["code"]
        except Exception:
            pass
        return message, code
