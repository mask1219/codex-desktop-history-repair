from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
import sys
from unittest.mock import patch
from urllib import error as urlerror

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import ContinuationMode, ProviderRequest, ResponsesApiProviderClient  # noqa: E402


class FakeResponse:
    def __init__(self, *, content_type: str, body: bytes, sse_lines: list[bytes] | None = None):
        self.headers = {"Content-Type": content_type}
        self._body = body
        self._sse_lines = sse_lines or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._sse_lines)


class ResponsesApiProviderClientTests(unittest.TestCase):
    def test_sse_stream_success(self):
        received_requests: list[dict] = []
        sse_lines = [
            f'data: {json.dumps({"type": "response.output_text.delta", "delta": "hello "})}\n'.encode("utf-8"),
            b"\n",
            f'data: {json.dumps({"type": "response.output_text.delta", "delta": "world"})}\n'.encode("utf-8"),
            b"\n",
            f'data: {json.dumps({"type": "response.completed", "response": {"id": "resp_sse_1"}})}\n'.encode("utf-8"),
            b"\n",
            b"data: [DONE]\n",
            b"\n",
        ]

        def fake_urlopen(req, timeout):
            received_requests.append(json.loads(req.data.decode("utf-8")))
            return FakeResponse(content_type="text/event-stream", body=b"", sse_lines=sse_lines)

        with patch("history_repair.providers.urlrequest.urlopen", side_effect=fake_urlopen):
            client = ResponsesApiProviderClient(
                base_url="https://example.test",
                api_key="test-key",
            )
            request = ProviderRequest(
                thread_id="thread_1",
                model="gpt-5.4",
                continuation_mode=ContinuationMode.REMOTE_CHAIN,
                messages=[{"role": "user", "content": "继续"}],
                previous_response_id="resp_prev",
                instructions="你是助手",
            )
            events = list(client.stream(request))
            self.assertEqual([event.kind for event in events], ["delta", "delta", "done"])
            self.assertEqual(events[0].text, "hello ")
            self.assertEqual(events[1].text, "world")
            self.assertEqual(events[2].remote_response_id, "resp_sse_1")
            self.assertEqual(received_requests[0]["previous_response_id"], "resp_prev")
            self.assertEqual(received_requests[0]["instructions"], "你是助手")
            self.assertEqual(received_requests[0]["input"][0]["content"], "继续")

    def test_http_error_yields_error_event(self):
        def fake_urlopen(req, timeout):
            raise urlerror.HTTPError(
                url=str(req.full_url),
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"bad request","code":"bad_request"}}'),
            )

        with patch("history_repair.providers.urlrequest.urlopen", side_effect=fake_urlopen):
            client = ResponsesApiProviderClient(
                base_url="https://example.test",
                api_key="test-key",
            )
            request = ProviderRequest(
                thread_id="thread_2",
                model="gpt-5.4",
                continuation_mode=ContinuationMode.LOCAL_REBUILD,
                messages=[{"role": "user", "content": "test"}],
            )
            events = list(client.stream(request))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "error")
            self.assertEqual(events[0].error_code, "bad_request")
            self.assertEqual(events[0].error_message, "bad request")

    def test_json_response_non_stream(self):
        def fake_urlopen(req, timeout):
            return FakeResponse(
                content_type="application/json",
                body=json.dumps(
                    {
                        "id": "resp_json_1",
                        "output": [
                            {
                                "content": [
                                    {"type": "output_text", "text": "from-json"},
                                ]
                            }
                        ],
                    }
                ).encode("utf-8"),
            )

        with patch("history_repair.providers.urlrequest.urlopen", side_effect=fake_urlopen):
            client = ResponsesApiProviderClient(
                base_url="https://example.test",
                api_key="test-key",
            )
            request = ProviderRequest(
                thread_id="thread_3",
                model="gpt-5.4",
                continuation_mode=ContinuationMode.LOCAL_REBUILD,
                messages=[{"role": "user", "content": "json"}],
            )
            events = list(client.stream(request))
            self.assertEqual([event.kind for event in events], ["delta", "done"])
            self.assertEqual(events[0].text, "from-json")
            self.assertEqual(events[1].remote_response_id, "resp_json_1")


if __name__ == "__main__":
    unittest.main()
