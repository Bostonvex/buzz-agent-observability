"""Loopback-only, content-blind OpenAI-compatible timing proxy.

Request bodies and authorization headers are streamed to one configured
upstream and are never parsed, logged, or stored. Response inspection is
bounded and extracts only timing and documented usage counters.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import queue
import re
import signal
import socket
import ssl
import stat
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit
from urllib.request import Request, urlopen

PROXY_VERSION = "0.1.0"
DEFAULT_ALLOWED_PATHS = frozenset(
    {"/v1/chat/completions", "/v1/completions", "/v1/responses"}
)
MAX_CONTEXT_BYTES = 8 * 1024
MAX_INSPECT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024 * 1024
COPY_BYTES = 64 * 1024
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
CONTEXT_HEADER_PREFIX = "x-buzz-telemetry-"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@=-]{0,255}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_identifier(value: Any, fallback: str | None = None) -> str | None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        return fallback
    return value


def _safe_label(value: Any, fallback: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if not candidate or len(candidate) > maximum or CONTROL.search(candidate):
        return fallback
    return candidate


def _hmac_identifier(salt: str, namespace: str, value: str) -> str:
    digest = hmac.new(
        salt.encode("utf-8"),
        f"{namespace}\0{value}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:30]
    return f"h_{encoded}"


def _read_private_file(path: str | Path, label: str, minimum: int) -> str:
    private_path = Path(path).expanduser()
    if private_path.is_symlink():
        raise ValueError(f"{label} file must not be a symbolic link")
    details = private_path.stat()
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError(f"{label} file must be regular and mode 0600 or stricter")
    value = private_path.read_text(encoding="ascii").strip()
    if len(value) < minimum or len(value) > 512:
        raise ValueError(f"{label} file contains an invalid value")
    return value


def _loopback_url(value: str, *, expected_path: str | None = None) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("URL must use HTTP on a loopback hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL must not contain credentials, a query, or a fragment")
    if expected_path is not None and parsed.path != expected_path:
        raise ValueError(f"URL path must be {expected_path}")
    return parsed


def _upstream_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("upstream URL must use HTTP or HTTPS and include a hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("upstream URL must not contain credentials, a query, or a fragment")
    return parsed


@dataclass(frozen=True)
class RequestContext:
    agent_id: str
    display_name: str
    harness: str | None
    model: str | None
    endpoint_id: str | None
    session_id: str | None
    turn_id: str | None
    correlation: str


class CorrelationRegistry:
    """Tracks normalized active ACP turns supplied by an authenticated observer."""

    _FIELDS = frozenset(
        {
            "context_id",
            "agent_id",
            "display_name",
            "harness",
            "model",
            "endpoint_id",
            "session_id",
            "turn_id",
        }
    )

    def __init__(self, fallback: RequestContext, maximum: int = 256) -> None:
        self.fallback = fallback
        self.maximum = max(1, min(maximum, 10_000))
        self._active: OrderedDict[str, RequestContext] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if value is None:
            return None
        validated = _safe_identifier(value)
        if validated is None:
            raise ValueError("invalid context identifier")
        return validated

    def start(self, value: Any) -> int:
        if not isinstance(value, dict) or set(value) != self._FIELDS:
            raise ValueError("invalid context shape")
        context_id = self._optional_identifier(value["context_id"])
        agent_id = self._optional_identifier(value["agent_id"])
        session_id = self._optional_identifier(value["session_id"])
        turn_id = self._optional_identifier(value["turn_id"])
        if context_id is None or agent_id is None or session_id is None or turn_id is None:
            raise ValueError("context identifiers are required")
        context = RequestContext(
            agent_id=agent_id,
            display_name=_safe_label(value["display_name"], "Unknown agent"),
            harness=self._optional_identifier(value["harness"]),
            model=self._optional_identifier(value["model"]),
            endpoint_id=self._optional_identifier(value["endpoint_id"]),
            session_id=session_id,
            turn_id=turn_id,
            correlation="exact",
        )
        with self._lock:
            self._active.pop(context_id, None)
            self._active[context_id] = context
            while len(self._active) > self.maximum:
                self._active.popitem(last=False)
            return len(self._active)

    def end(self, context_id: Any) -> int:
        validated = self._optional_identifier(context_id)
        if validated is None:
            raise ValueError("context id is required")
        with self._lock:
            self._active.pop(validated, None)
            return len(self._active)

    def resolve(self, context_id: str | None = None) -> RequestContext:
        with self._lock:
            if context_id and context_id in self._active:
                return self._active[context_id]
            if len(self._active) == 1:
                return next(iter(self._active.values()))
            if len(self._active) > 1:
                return RequestContext(
                    **{
                        **self.fallback.__dict__,
                        "session_id": None,
                        "turn_id": None,
                        "correlation": "ambiguous",
                    }
                )
            return self.fallback

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)


class EventSink(Protocol):
    def enqueue(self, event: dict[str, Any]) -> bool: ...

    def close(self, deadline: float = 0.25) -> None: ...


class NullEventSink:
    def enqueue(self, event: dict[str, Any]) -> bool:
        del event
        return False

    def close(self, deadline: float = 0.25) -> None:
        del deadline


class CollectorEventSink:
    """Bounded asynchronous collector delivery that never blocks proxy traffic."""

    def __init__(
        self,
        collector_url: str,
        token: str,
        *,
        capacity: int = 512,
        timeout: float = 0.2,
    ) -> None:
        _loopback_url(collector_url, expected_path="/api/v1/events")
        self.collector_url = collector_url
        self.token = token
        self.timeout = max(0.01, min(timeout, 5.0))
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, capacity))
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="buzz-proxy-telemetry", daemon=True)
        self.thread.start()

    def enqueue(self, event: dict[str, Any]) -> bool:
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.queue.put_nowait(event)
                return True
            except (queue.Empty, queue.Full):
                return False

    def _run(self) -> None:
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                first = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < 50:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                body = json.dumps(batch, separators=(",", ":")).encode("utf-8")
                request = Request(
                    self.collector_url,
                    data=body,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(request, timeout=self.timeout) as response:
                    response.read(1)
            except Exception:
                pass
            finally:
                for _ in batch:
                    self.queue.task_done()

    def close(self, deadline: float = 0.25) -> None:
        self.stopping.set()
        self.thread.join(timeout=max(0.0, deadline))


class ResponseInspector:
    """Bounded response metadata parser; retained buffers are never logged or stored."""

    def __init__(self, content_type: str) -> None:
        self.streaming = "text/event-stream" in content_type.lower()
        self.first_generated_at: float | None = None
        self.usage: dict[str, int] = {}
        self._buffer = bytearray()
        self._event_data: list[bytes] = []
        self._inspect_enabled = True

    @staticmethod
    def _counter(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        mappings = {
            "input_tokens": ("input_tokens", "prompt_tokens"),
            "output_tokens": ("output_tokens", "completion_tokens"),
        }
        for target, names in mappings.items():
            for name in names:
                found = self._counter(value.get(name))
                if found is not None:
                    self.usage[target] = found
                    break
        input_details = value.get("input_tokens_details") or value.get("prompt_tokens_details")
        output_details = value.get("output_tokens_details") or value.get(
            "completion_tokens_details"
        )
        if isinstance(input_details, dict):
            found = self._counter(input_details.get("cached_tokens"))
            if found is not None:
                self.usage["cached_tokens"] = found
        if isinstance(output_details, dict):
            found = self._counter(output_details.get("reasoning_tokens"))
            if found is not None:
                self.usage["reasoning_tokens"] = found

    @staticmethod
    def _generated(value: dict[str, Any]) -> bool:
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                text = choice.get("text")
                delta = choice.get("delta")
                if isinstance(text, str) and text:
                    return True
                if isinstance(delta, dict):
                    if isinstance(delta.get("content"), str) and delta["content"]:
                        return True
                    if delta.get("tool_calls") or delta.get("function_call"):
                        return True
        event_type = value.get("type")
        if isinstance(event_type, str) and event_type.endswith(".delta"):
            return bool(value.get("delta")) and event_type in {
                "response.output_text.delta",
                "response.function_call_arguments.delta",
                "response.reasoning_summary_text.delta",
                "response.refusal.delta",
            }
        return False

    def _inspect_object(self, value: Any, observed_at: float) -> None:
        if not isinstance(value, dict):
            return
        self._usage(value.get("usage"))
        response = value.get("response")
        if isinstance(response, dict):
            self._usage(response.get("usage"))
        if self.first_generated_at is None and self._generated(value):
            self.first_generated_at = observed_at

    def _inspect_json(self, payload: bytes, observed_at: float) -> None:
        if not payload or payload == b"[DONE]" or len(payload) > MAX_INSPECT_BYTES:
            return
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        self._inspect_object(value, observed_at)

    def _consume_sse_line(self, line: bytes, observed_at: float) -> None:
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line:
            if self._event_data:
                self._inspect_json(b"\n".join(self._event_data), observed_at)
                self._event_data.clear()
            return
        if line.startswith(b"data:"):
            data = line[5:]
            if data.startswith(b" "):
                data = data[1:]
            if sum(map(len, self._event_data)) + len(data) <= MAX_INSPECT_BYTES:
                self._event_data.append(data)
            else:
                self._event_data.clear()

    def feed(self, chunk: bytes, observed_at: float) -> None:
        if not self._inspect_enabled:
            return
        if self.streaming:
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_INSPECT_BYTES:
                newline = self._buffer.rfind(b"\n")
                self._buffer = self._buffer[newline + 1 :] if newline >= 0 else bytearray()
                self._event_data.clear()
            while (newline := self._buffer.find(b"\n")) >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                self._consume_sse_line(line, observed_at)
        elif len(self._buffer) + len(chunk) <= MAX_INSPECT_BYTES:
            self._buffer.extend(chunk)
        else:
            self._buffer.clear()
            self._inspect_enabled = False

    def finish(self, observed_at: float) -> None:
        if self.streaming:
            if self._buffer:
                self._consume_sse_line(bytes(self._buffer), observed_at)
            if self._event_data:
                self._inspect_json(b"\n".join(self._event_data), observed_at)
            self._buffer.clear()
            self._event_data.clear()
        elif self._inspect_enabled:
            self._inspect_json(bytes(self._buffer), observed_at)
            self._buffer.clear()


@dataclass
class ProxyState:
    upstream: SplitResult
    allowed_paths: frozenset[str]
    registry: CorrelationRegistry
    sink: EventSink
    context_token: str | None
    instance_id: str = "proxy-instance"
    connect_timeout: float = 10.0
    read_timeout: float = 300.0

    def event(
        self,
        event_type: str,
        context: RequestContext,
        span_id: str,
        attributes: dict[str, Any],
        observed: float,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "observed_at": _utc_now(),
            "monotonic_offset_ms": observed * 1_000,
            "producer": {
                "name": "buzz-openai-proxy",
                "version": PROXY_VERSION,
                "instance_id": self.instance_id,
            },
            "agent": {"id": context.agent_id, "display_name": context.display_name},
            "harness": context.harness,
            "model": context.model,
            "endpoint_id": context.endpoint_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "span_id": span_id,
            "parent_span_id": context.turn_id,
            "attributes": attributes,
        }


class ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _connection(state: ProxyState) -> http.client.HTTPConnection:
    port = state.upstream.port
    if state.upstream.scheme == "https":
        return http.client.HTTPSConnection(
            state.upstream.hostname,
            port or 443,
            timeout=state.connect_timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(
        state.upstream.hostname,
        port or 80,
        timeout=state.connect_timeout,
    )


def _upstream_path(upstream: SplitResult, request_path: str) -> str:
    base = upstream.path.rstrip("/")
    if not base or request_path == base or request_path.startswith(base + "/"):
        return request_path
    return base + request_path


def make_proxy_handler(state: ProxyState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"BuzzOpenAIProxy/{PROXY_VERSION}"
        sys_version = ""

        def log_message(self, message: str, *args: Any) -> None:
            del message, args

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _context(self) -> None:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {state.context_token}" if state.context_token else ""
            if not expected or not hmac.compare_digest(supplied, expected):
                self.close_connection = True
                self._json(HTTPStatus.UNAUTHORIZED, {"error": {"code": "invalid_token"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 2 or length > MAX_CONTEXT_BYTES:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"code": "invalid_body"}})
                return
            try:
                submitted = json.loads(self.rfile.read(length))
                action = submitted.get("action") if isinstance(submitted, dict) else None
                if action == "start" and set(submitted) == {"action", "context"}:
                    active = state.registry.start(submitted["context"])
                elif action == "end" and set(submitted) == {"action", "context_id"}:
                    active = state.registry.end(submitted["context_id"])
                else:
                    raise ValueError("invalid action")
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": {"code": "invalid_context"}})
                return
            self._json(HTTPStatus.OK, {"status": "ok", "active_contexts": active})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            request_target = urlsplit(self.path)
            if request_target.path == "/__buzz/context":
                self._context()
                return
            if request_target.path not in state.allowed_paths:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "path_not_allowed"}})
                return
            self._proxy(request_target.path, request_target.query)

        def _proxy(self, path: str, query: str) -> None:
            try:
                content_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                content_length = -1
            if content_length < 0 or content_length > MAX_REQUEST_BYTES:
                self._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                    if content_length > MAX_REQUEST_BYTES
                    else HTTPStatus.LENGTH_REQUIRED,
                    {"error": {"code": "invalid_content_length"}},
                )
                return

            context_header = next(
                (
                    value
                    for name, value in self.headers.items()
                    if name.lower() == "x-buzz-telemetry-context-id"
                ),
                None,
            )
            requested_context = _safe_identifier(context_header)
            context = state.registry.resolve(requested_context)
            span_id = str(uuid.uuid4())
            started = time.monotonic()
            state.sink.enqueue(
                state.event(
                    "model.request_started",
                    context,
                    span_id,
                    {
                        "correlation": context.correlation,
                        "measurement_quality": "exact",
                    },
                    started,
                )
            )

            upstream = _connection(state)
            connected: float | None = None
            first_byte: float | None = None
            upstream_status: int | None = None
            response_started = False
            try:
                upstream.connect()
                connected = time.monotonic()
                target = _upstream_path(state.upstream, path)
                if query:
                    target += "?" + query
                upstream.putrequest("POST", target, skip_host=True, skip_accept_encoding=True)
                host = state.upstream.hostname or ""
                default_port = 443 if state.upstream.scheme == "https" else 80
                host_header = (
                    host
                    if (state.upstream.port or default_port) == default_port
                    else f"{host}:{state.upstream.port}"
                )
                upstream.putheader("Host", host_header)
                request_connection_headers = {
                    item.strip().lower()
                    for item in self.headers.get("Connection", "").split(",")
                    if item.strip()
                }
                for name, value in self.headers.items():
                    lower = name.lower()
                    if (
                        lower in HOP_BY_HOP_HEADERS
                        or lower in request_connection_headers
                        or lower in {"host", "content-length", "accept-encoding"}
                        or lower.startswith(CONTEXT_HEADER_PREFIX)
                    ):
                        continue
                    upstream.putheader(name, value)
                upstream.putheader("Content-Length", str(content_length))
                upstream.putheader("Accept-Encoding", "identity")
                upstream.endheaders()
                remaining = content_length
                while remaining:
                    chunk = self.rfile.read(min(COPY_BYTES, remaining))
                    if not chunk:
                        raise ConnectionError("downstream request ended early")
                    upstream.send(chunk)
                    remaining -= len(chunk)

                response = upstream.getresponse()
                upstream_status = response.status
                upstream.sock.settimeout(state.read_timeout) if upstream.sock else None
                content_type = response.getheader("Content-Type", "")
                inspector = ResponseInspector(content_type)
                first_token_emitted = False
                response_length = response.getheader("Content-Length")
                use_chunked = response_length is None

                response_connection_headers = {
                    item.strip().lower()
                    for item in (response.getheader("Connection") or "").split(",")
                    if item.strip()
                }
                self.send_response(response.status)
                for name, value in response.getheaders():
                    lower = name.lower()
                    if (
                        lower in HOP_BY_HOP_HEADERS
                        or lower in response_connection_headers
                        or lower == "content-length"
                    ):
                        continue
                    self.send_header(name, value)
                if response_length is not None:
                    self.send_header("Content-Length", response_length)
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                response_started = True

                while True:
                    chunk = response.read1(COPY_BYTES)
                    if not chunk:
                        break
                    observed = time.monotonic()
                    if first_byte is None:
                        first_byte = observed
                    inspector.feed(chunk, observed)
                    if inspector.first_generated_at is not None and not first_token_emitted:
                        first_token_emitted = True
                        state.sink.enqueue(
                            state.event(
                                "model.first_token",
                                context,
                                span_id,
                                {
                                    "elapsed_ms": (inspector.first_generated_at - started) * 1_000,
                                    "correlation": context.correlation,
                                    "measurement_quality": "exact",
                                },
                                inspector.first_generated_at,
                            )
                        )
                    if use_chunked:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                if use_chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                finished = time.monotonic()
                inspector.finish(finished)

                common: dict[str, Any] = {
                    "duration_ms": (finished - started) * 1_000,
                    "http_status": response.status,
                    "correlation": context.correlation,
                    "measurement_quality": "exact",
                }
                if connected is not None:
                    common["connection_ms"] = (connected - started) * 1_000
                if first_byte is not None:
                    common["first_byte_ms"] = (first_byte - started) * 1_000
                if inspector.first_generated_at is not None:
                    if not first_token_emitted:
                        state.sink.enqueue(
                            state.event(
                                "model.first_token",
                                context,
                                span_id,
                                {
                                    "elapsed_ms": (inspector.first_generated_at - started) * 1_000,
                                    "correlation": context.correlation,
                                    "measurement_quality": "exact",
                                },
                                inspector.first_generated_at,
                            )
                        )
                    common["decode_ms"] = max(
                        0.0, (finished - inspector.first_generated_at) * 1_000
                    )
                common.update(inspector.usage)
                event_type = "model.completed" if response.status < 400 else "model.failed"
                if event_type == "model.failed":
                    common.update(
                        {
                            "error_category": "upstream_http",
                            "error_code": f"http_{response.status}",
                        }
                    )
                state.sink.enqueue(state.event(event_type, context, span_id, common, finished))
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                upstream.close()
                finished = time.monotonic()
                attributes: dict[str, Any] = {
                    "duration_ms": (finished - started) * 1_000,
                    "http_status": upstream_status or 499,
                    "error_category": "client_cancelled",
                    "error_code": "downstream_disconnect",
                    "correlation": context.correlation,
                    "measurement_quality": "exact",
                }
                if connected is not None:
                    attributes["connection_ms"] = (connected - started) * 1_000
                if first_byte is not None:
                    attributes["first_byte_ms"] = (first_byte - started) * 1_000
                state.sink.enqueue(
                    state.event("model.failed", context, span_id, attributes, finished)
                )
            except Exception:
                upstream.close()
                finished = time.monotonic()
                attributes = {
                    "duration_ms": (finished - started) * 1_000,
                    "http_status": upstream_status or 502,
                    "error_category": "upstream_transport",
                    "error_code": "connection_or_protocol_failure",
                    "correlation": context.correlation,
                    "measurement_quality": "exact",
                }
                if connected is not None:
                    attributes["connection_ms"] = (connected - started) * 1_000
                if first_byte is not None:
                    attributes["first_byte_ms"] = (first_byte - started) * 1_000
                state.sink.enqueue(
                    state.event("model.failed", context, span_id, attributes, finished)
                )
                if not response_started:
                    self._json(
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "error": {
                                "type": "proxy_error",
                                "code": "upstream_unavailable",
                                "message": "The configured model endpoint is unavailable.",
                            }
                        },
                    )
            finally:
                upstream.close()

    return Handler


def create_proxy_server(
    host: str,
    port: int,
    state: ProxyState,
) -> ProxyHTTPServer:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError("proxy host must resolve to loopback") from error
    if not addresses or any(not address.startswith("127.") and address != "::1" for address in addresses):
        raise ValueError("proxy may bind only to a loopback host")
    return ProxyHTTPServer((host, port), make_proxy_handler(state))


def build_state(args: argparse.Namespace) -> ProxyState:
    upstream = _upstream_url(args.upstream)
    instance_id = str(uuid.uuid4())
    context_token: str | None = None
    sink: EventSink = NullEventSink()
    salt = "disabled-telemetry-salt"
    try:
        context_token = _read_private_file(args.token_file, "token", 32)
        salt = _read_private_file(args.identity_salt_file, "identity salt", 16)
        sink = CollectorEventSink(args.collector_url, context_token)
    except (OSError, ValueError):
        pass
    identity_material = args.agent_id or f"proxy:{instance_id}"
    fallback = RequestContext(
        agent_id=_hmac_identifier(salt, "agent", identity_material),
        display_name=_safe_label(args.display_name, "Unattributed model proxy"),
        harness=_safe_identifier(args.harness),
        model=_safe_identifier(args.model),
        endpoint_id=_safe_identifier(args.endpoint_id),
        session_id=None,
        turn_id=None,
        correlation="ambiguous" if args.agent_id else "unavailable",
    )
    return ProxyState(
        upstream=upstream,
        allowed_paths=frozenset(args.allowed_path),
        registry=CorrelationRegistry(fallback),
        sink=sink,
        context_token=context_token,
        instance_id=instance_id,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optional content-blind OpenAI timing proxy")
    parser.add_argument(
        "--upstream",
        default=os.environ.get("BUZZ_MODEL_PROXY_UPSTREAM"),
        required="BUZZ_MODEL_PROXY_UPSTREAM" not in os.environ,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--allowed-path", action="append", default=list(DEFAULT_ALLOWED_PATHS))
    parser.add_argument(
        "--collector-url",
        default=os.environ.get(
            "BUZZ_TELEMETRY_URL", "http://127.0.0.1:7900/api/v1/events"
        ),
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get(
            "BUZZ_TELEMETRY_TOKEN_FILE",
            "~/.config/buzz-agent-observability/ingest-token",
        ),
    )
    parser.add_argument(
        "--identity-salt-file",
        default=os.environ.get(
            "BUZZ_TELEMETRY_IDENTITY_SALT_FILE",
            "~/.config/buzz-agent-observability/identity-salt",
        ),
    )
    parser.add_argument("--agent-id", default=os.environ.get("BUZZ_TELEMETRY_AGENT_ID"))
    parser.add_argument("--display-name", default=os.environ.get("BUZZ_ACP_DISPLAY_NAME"))
    parser.add_argument("--harness", default=os.environ.get("BUZZ_TELEMETRY_HARNESS"))
    parser.add_argument("--model", default=os.environ.get("BUZZ_TELEMETRY_MODEL"))
    parser.add_argument("--endpoint-id", default=os.environ.get("BUZZ_TELEMETRY_ENDPOINT_ID"))
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = build_state(args)
    server = create_proxy_server(args.host, args.port, state)
    host, port = server.server_address
    print(f"Buzz OpenAI timing proxy listening on http://{host}:{port}", flush=True)

    stopping = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        state.sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
