from __future__ import annotations

import http.client
import json
import socket
import statistics
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from collector.schema import validate_event
from proxy.openai_proxy import (
    CorrelationRegistry,
    ProxyState,
    RequestContext,
    ResponseInspector,
    _upstream_path,
    _upstream_url,
    create_proxy_server,
)


PRIVATE_REQUEST = "synthetic-private-request-material"
PRIVATE_RESPONSE = "synthetic-private-response-material"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def enqueue(self, event: dict[str, Any]) -> bool:
        with self.lock:
            self.events.append(event)
        return True

    def close(self, deadline: float = 0.25) -> None:
        del deadline

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["event_type"] == event_type]


class MockOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    request_headers: list[dict[str, str]] = []
    request_sizes: list[int] = []
    cancelled = threading.Event()

    def log_message(self, message: str, *args: Any) -> None:
        del message, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        remaining = length
        received = 0
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            received += len(chunk)
            remaining -= len(chunk)
        type(self).request_headers.append(dict(self.headers.items()))
        type(self).request_sizes.append(received)
        mode = self.headers.get("X-Test-Mode", "nonstream")
        if mode in {"stream", "tool", "cancel", "anthropic-stream", "anthropic-error"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if mode == "anthropic-stream":
                chunks = [
                    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":1,"cache_read_input_tokens":4}}}\n\n',
                    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"anthropic-private-response"}}\n\n',
                    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":6}}\n\n',
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                ]
            elif mode == "anthropic-error":
                chunks = [
                    b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"private"}}\n\n',
                ]
            elif mode == "tool":
                chunks = [
                    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}\n\n',
                    b'data: {"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n',
                    b"data: [DONE]\n\n",
                ]
            else:
                chunks = [
                    b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
                    (
                        b'data: {"choices":[{"delta":{"content":"'
                        + PRIVATE_RESPONSE.encode("ascii")
                        + b'"}}]}\n\n'
                    ),
                    (
                        b'data: {"choices":[],"usage":{"prompt_tokens":7,'
                        b'"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2},'
                        b'"completion_tokens_details":{"reasoning_tokens":1}}}\n\n'
                    ),
                    b"data: [DONE]\n\n",
                ]
            if mode == "cancel":
                chunks.extend(
                    b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                    for _ in range(200)
                )
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                type(self).cancelled.set()
            return
        if mode == "error":
            body = json.dumps(
                {"error": {"message": PRIVATE_RESPONSE, "type": "rate_limit_error"}},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(429)
        else:
            body = json.dumps(
                {
                    "id": "safe-response-id",
                    "choices": [{"message": {"content": PRIVATE_RESPONSE}}],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 3},
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Upstream-Test", "preserved")
        self.end_headers()
        self.wfile.write(body)


class ModelProxyTests(unittest.TestCase):
    token = "context-token-" + "x" * 32

    def setUp(self) -> None:
        MockOpenAIHandler.request_headers.clear()
        MockOpenAIHandler.request_sizes.clear()
        MockOpenAIHandler.cancelled.clear()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.sink = RecordingSink()
        fallback = RequestContext(
            agent_id="agent-fallback",
            display_name="Fallback agent",
            harness="test-harness",
            model="test-model",
            endpoint_id="test-endpoint",
            session_id=None,
            turn_id=None,
            correlation="unavailable",
        )
        self.registry = CorrelationRegistry(fallback)
        self.state = ProxyState(
            upstream=_upstream_url(f"http://127.0.0.1:{self.upstream.server_address[1]}"),
            allowed_paths=frozenset(
                {"/v1/chat/completions", "/v1/responses", "/v1/messages"}
            ),
            registry=self.registry,
            sink=self.sink,
            context_token=self.token,
            connect_timeout=2,
            read_timeout=2,
            instance_id="test-proxy-instance",
        )
        self.proxy = create_proxy_server("127.0.0.1", 0, self.state)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.proxy_url = f"http://127.0.0.1:{self.proxy.server_address[1]}"

    def tearDown(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.proxy_thread.join(timeout=2)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)

    def context(self, context_id: str = "context-one", turn_id: str = "turn-one") -> None:
        body = json.dumps(
            {
                "action": "start",
                "context": {
                    "context_id": context_id,
                    "agent_id": "agent-alpha",
                    "display_name": "Agent Alpha",
                    "harness": "deepseek",
                    "model": "model-alpha",
                    "endpoint_id": "endpoint-alpha",
                    "session_id": "session-alpha",
                    "turn_id": turn_id,
                },
            }
        ).encode("utf-8")
        request = Request(
            self.proxy_url + "/__buzz/context",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)

    def post(
        self,
        *,
        mode: str = "nonstream",
        body: bytes | None = None,
        context_id: str | None = None,
        path: str = "/v1/chat/completions",
    ) -> tuple[int, bytes, http.client.HTTPMessage]:
        terminal_before = len(self.sink.by_type("model.completed")) + len(
            self.sink.by_type("model.failed")
        )
        submitted = body or json.dumps({"input": PRIVATE_REQUEST}).encode("utf-8")
        headers = {
            "Authorization": "Bearer upstream-private-value",
            "Content-Type": "application/json",
            "X-Test-Mode": mode,
        }
        if context_id:
            headers["X-Buzz-Telemetry-Context-Id"] = context_id
        request = Request(self.proxy_url + path, data=submitted, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                result = (response.status, response.read(), response.headers)
        except HTTPError as error:
            try:
                result = (error.code, error.read(), error.headers)
            finally:
                error.close()
        deadline = time.monotonic() + 1
        while (
            len(self.sink.by_type("model.completed")) + len(self.sink.by_type("model.failed"))
            <= terminal_before
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        return result

    def test_nonstreaming_parity_usage_and_content_exclusion(self) -> None:
        self.context()
        status, body, headers = self.post(context_id="context-one")
        self.assertEqual(status, 200)
        self.assertIn(PRIVATE_RESPONSE.encode("ascii"), body)
        self.assertEqual(headers["X-Upstream-Test"], "preserved")
        forwarded = {key.lower(): value for key, value in MockOpenAIHandler.request_headers[-1].items()}
        self.assertEqual(forwarded["authorization"], "Bearer upstream-private-value")
        self.assertNotIn("x-buzz-telemetry-context-id", forwarded)

        completed = self.sink.by_type("model.completed")[-1]
        self.assertEqual(completed["turn_id"], "turn-one")
        self.assertEqual(completed["attributes"]["correlation"], "exact")
        self.assertEqual(completed["attributes"]["input_tokens"], 11)
        self.assertEqual(completed["attributes"]["output_tokens"], 5)
        self.assertEqual(completed["attributes"]["cached_tokens"], 3)
        self.assertEqual(completed["attributes"]["reasoning_tokens"], 2)
        self.assertIn("connection_ms", completed["attributes"])
        self.assertIn("first_byte_ms", completed["attributes"])
        for event in self.sink.events:
            validate_event(event)
        retained = json.dumps(self.sink.events)
        self.assertNotIn(PRIVATE_REQUEST, retained)
        self.assertNotIn(PRIVATE_RESPONSE, retained)
        self.assertNotIn("upstream-private-value", retained)

    def test_streaming_bytes_ttft_decode_and_usage_are_preserved(self) -> None:
        self.context()
        status, body, _ = self.post(mode="stream")
        self.assertEqual(status, 200)
        self.assertIn(b"data: [DONE]\n\n", body)
        self.assertIn(PRIVATE_RESPONSE.encode("ascii"), body)
        first = self.sink.by_type("model.first_token")[-1]
        completed = self.sink.by_type("model.completed")[-1]
        self.assertGreater(first["attributes"]["elapsed_ms"], 0)
        self.assertLessEqual(
            completed["attributes"]["first_byte_ms"], first["attributes"]["elapsed_ms"]
        )
        self.assertGreaterEqual(completed["attributes"]["decode_ms"], 0)
        self.assertEqual(completed["attributes"]["output_tokens"], 3)
        self.assertEqual(completed["attributes"]["reasoning_tokens"], 1)

    def test_streaming_tool_call_counts_as_first_generated_chunk(self) -> None:
        self.context()
        status, body, _ = self.post(mode="tool")
        self.assertEqual(status, 200)
        self.assertIn(b"tool_calls", body)
        self.assertEqual(len(self.sink.by_type("model.first_token")), 1)

    def test_anthropic_streaming_bytes_usage_and_ttft_are_preserved(self) -> None:
        self.context()
        status, body, _ = self.post(mode="anthropic-stream", path="/v1/messages")
        self.assertEqual(status, 200)
        self.assertIn(b"anthropic-private-response", body)
        completed = self.sink.by_type("model.completed")[-1]
        self.assertEqual(completed["attributes"]["input_tokens"], 12)
        self.assertEqual(completed["attributes"]["output_tokens"], 6)
        self.assertEqual(completed["attributes"]["cached_tokens"], 4)
        self.assertGreater(completed["attributes"]["decode_ms"], 0)
        self.assertEqual(len(self.sink.by_type("model.first_token")), 1)
        self.assertNotIn("anthropic-private-response", json.dumps(self.sink.events))

    def test_anthropic_stream_error_is_a_safe_model_failure(self) -> None:
        self.context()
        status, body, _ = self.post(mode="anthropic-error", path="/v1/messages")
        self.assertEqual(status, 200)
        self.assertIn(b"overloaded_error", body)
        failed = self.sink.by_type("model.failed")[-1]
        self.assertEqual(failed["attributes"]["error_category"], "upstream_stream")
        self.assertEqual(failed["attributes"]["error_code"], "overloaded_error")
        self.assertNotIn("private", json.dumps(failed))

    def test_upstream_error_status_and_body_are_preserved_without_content_capture(self) -> None:
        self.context()
        status, body, _ = self.post(mode="error")
        self.assertEqual(status, 429)
        self.assertIn(PRIVATE_RESPONSE.encode("ascii"), body)
        failed = self.sink.by_type("model.failed")[-1]
        self.assertEqual(failed["attributes"]["http_status"], 429)
        self.assertEqual(failed["attributes"]["error_code"], "http_429")
        self.assertNotIn(PRIVATE_RESPONSE, json.dumps(failed))

    def test_large_request_streams_to_upstream(self) -> None:
        body = (PRIVATE_REQUEST.encode("ascii") + b"-") * 80_000
        status, _, _ = self.post(body=body)
        self.assertEqual(status, 200)
        self.assertEqual(MockOpenAIHandler.request_sizes[-1], len(body))
        self.assertNotIn(PRIVATE_REQUEST, json.dumps(self.sink.events))

    def test_multiple_active_turns_are_ambiguous_unless_context_header_selects_one(self) -> None:
        self.context("context-one", "turn-one")
        self.context("context-two", "turn-two")
        status, _, _ = self.post()
        self.assertEqual(status, 200)
        ambiguous = self.sink.by_type("model.completed")[-1]
        self.assertEqual(ambiguous["attributes"]["correlation"], "ambiguous")
        self.assertIsNone(ambiguous["turn_id"])

        status, _, _ = self.post(context_id="context-two")
        self.assertEqual(status, 200)
        exact = self.sink.by_type("model.completed")[-1]
        self.assertEqual(exact["attributes"]["correlation"], "exact")
        self.assertEqual(exact["turn_id"], "turn-two")

    def test_context_endpoint_requires_token_and_allowed_paths_are_fixed(self) -> None:
        request = Request(
            self.proxy_url + "/__buzz/context",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        status, body, _ = self.post(path="/v1/files")
        self.assertEqual(status, 404)
        self.assertIn(b"path_not_allowed", body)

    def test_client_disconnect_cancels_upstream_stream_and_emits_safe_failure(self) -> None:
        self.context()
        connection = socket.create_connection(("127.0.0.1", self.proxy.server_address[1]), timeout=2)
        request_body = b"{}"
        request = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Test-Mode: cancel\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(request_body)}\r\n\r\n".encode("ascii")
            + request_body
        )
        connection.sendall(request)
        connection.recv(1024)
        connection.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            failures = self.sink.by_type("model.failed")
            if failures and failures[-1]["attributes"]["error_code"] == "downstream_disconnect":
                break
            time.sleep(0.02)
        self.assertTrue(failures)
        self.assertEqual(failures[-1]["attributes"]["error_category"], "client_cancelled")

    def test_non_loopback_bind_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_proxy_server("0.0.0.0", 0, self.state)

    def test_synthetic_loopback_added_p50_is_bounded(self) -> None:
        body = b"{}"
        direct_url = f"http://127.0.0.1:{self.upstream.server_address[1]}/v1/chat/completions"

        def direct() -> None:
            request = Request(direct_url, data=body, method="POST")
            with urlopen(request, timeout=2) as response:
                response.read()

        for _ in range(3):
            direct()
            self.post(body=body)
        direct_ms: list[float] = []
        proxy_ms: list[float] = []
        for _ in range(15):
            started = time.perf_counter()
            direct()
            direct_ms.append((time.perf_counter() - started) * 1_000)
        for _ in range(15):
            started = time.perf_counter()
            self.post(body=body)
            proxy_ms.append((time.perf_counter() - started) * 1_000)
        added_p50 = statistics.median(proxy_ms) - statistics.median(direct_ms)
        self.assertLess(added_p50, 50, f"synthetic added p50 was {added_p50:.3f} ms")


class ResponseInspectorTests(unittest.TestCase):
    def test_anthropic_sse_extracts_generated_delta_and_cumulative_usage(self) -> None:
        inspector = ResponseInspector("text/event-stream")
        payload = (
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":21,"output_tokens":1,"cache_read_input_tokens":5}}}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":8}}\n\n'
        )
        inspector.feed(payload[:73], 2.0)
        inspector.feed(payload[73:181], 2.1)
        inspector.feed(payload[181:], 2.2)
        inspector.finish(2.3)
        self.assertEqual(inspector.first_generated_at, 2.2)
        self.assertEqual(
            inspector.usage,
            {"input_tokens": 21, "output_tokens": 8, "cached_tokens": 5},
        )

    def test_prefixed_anthropic_upstream_path_is_joined_once(self) -> None:
        upstream = _upstream_url("https://example.test/api/anthropic")
        self.assertEqual(
            _upstream_path(upstream, "/v1/messages"),
            "/api/anthropic/v1/messages",
        )

    def test_responses_api_sse_extracts_delta_and_nested_usage_across_splits(self) -> None:
        inspector = ResponseInspector("text/event-stream")
        payload = (
            b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,'
            b'"output_tokens":4,"input_tokens_details":{"cached_tokens":2},'
            b'"output_tokens_details":{"reasoning_tokens":1}}}}\n\n'
        )
        inspector.feed(payload[:31], 1.0)
        inspector.feed(payload[31:79], 1.1)
        inspector.feed(payload[79:], 1.2)
        inspector.finish(1.3)
        self.assertEqual(inspector.first_generated_at, 1.1)
        self.assertEqual(
            inspector.usage,
            {
                "input_tokens": 9,
                "output_tokens": 4,
                "cached_tokens": 2,
                "reasoning_tokens": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
