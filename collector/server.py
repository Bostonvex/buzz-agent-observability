"""Loopback-only HTTP collector, query API, SSE feed, and static dashboard."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import queue
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from collector import __version__
from collector.schema import EventValidationError, validate_batch
from collector.storage import TelemetryStore

LOGGER = logging.getLogger("buzz_observability")
MAX_BODY_BYTES = 256 * 1024
MAX_BATCH_EVENTS = 100
MAX_QUERY_ROWS = 500


class SseBroker:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        summary = {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "observed_at": event["observed_at"],
            "agent_id": event["agent"]["id"],
            "agent_display_name": event["agent"]["display_name"],
            "harness": event["harness"],
            "model": event["model"],
            "turn_id": event["turn_id"],
        }
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(summary)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(summary)
                except (queue.Empty, queue.Full):
                    pass


@dataclass
class AppState:
    store: TelemetryStore
    ingest_token: str
    dashboard_dir: Path
    retention_days: int = 7

    def __post_init__(self) -> None:
        self.broker = SseBroker()
        self.stopping = threading.Event()
        self._last_retention: float | None = None
        self._retention_lock = threading.Lock()

    def maintain_retention(self) -> None:
        now = time.monotonic()
        if self._last_retention is not None and now - self._last_retention < 300:
            return
        with self._retention_lock:
            if self._last_retention is not None and now - self._last_retention < 300:
                return
            self.store.purge_expired_raw(retention_days=self.retention_days)
            self._last_retention = now


class CollectorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _bounded_limit(query: dict[str, list[str]], default: int = 100) -> int:
    raw = query.get("limit", [str(default)])[0]
    try:
        return max(1, min(MAX_QUERY_ROWS, int(raw)))
    except ValueError:
        return default


def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"BuzzAgentObservability/{__version__}"
        sys_version = ""

        def log_message(self, message: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.client_address[0], message % args)

        def _headers(self, content_type: str, content_length: int | None = None) -> None:
            self.send_header("Content-Type", content_type)
            if content_length is not None:
                self.send_header("Content-Length", str(content_length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, code: str, path: str | None = None) -> None:
            error: dict[str, Any] = {"code": code}
            if path:
                error["path"] = path
            self._send_json(status, {"error": error})

        def _serve_asset(self, name: str) -> None:
            asset = state.dashboard_dir / name
            try:
                body = asset.read_bytes()
            except (FileNotFoundError, OSError):
                self._send_error_json(HTTPStatus.NOT_FOUND, "not_found")
                return
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _serve_sse(self) -> None:
            subscriber = state.broker.subscribe()
            self.send_response(HTTPStatus.OK)
            self._headers("text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"event: ready\ndata: {\"status\":\"connected\"}\n\n")
                self.wfile.flush()
                while not state.stopping.is_set():
                    try:
                        event = subscriber.get(timeout=15)
                        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                        self.wfile.write(b"event: telemetry\ndata: " + payload + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                state.broker.unsubscribe(subscriber)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path in {"/", "/index.html"}:
                self._serve_asset("index.html")
            elif parsed.path == "/app.js":
                self._serve_asset("app.js")
            elif parsed.path == "/styles.css":
                self._serve_asset("styles.css")
            elif parsed.path == "/healthz":
                try:
                    self._send_json(HTTPStatus.OK, state.store.health())
                except Exception:
                    LOGGER.exception("health check failed")
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "unhealthy", "schema_version": 1})
            elif parsed.path == "/api/v1/agents":
                self._send_json(HTTPStatus.OK, {"agents": state.store.list_agents(limit=_bounded_limit(query))})
            elif parsed.path == "/api/v1/turns":
                self._send_json(HTTPStatus.OK, {"turns": state.store.list_turns(limit=_bounded_limit(query))})
            elif parsed.path == "/api/v1/live":
                self._serve_sse()
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not_found")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path != "/api/v1/events":
                self._send_error_json(HTTPStatus.NOT_FOUND, "not_found")
                return

            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {state.ingest_token}"
            if not hmac.compare_digest(supplied, expected):
                self.close_connection = True
                self._send_error_json(HTTPStatus.UNAUTHORIZED, "invalid_token")
                return

            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type_must_be_json")
                return

            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
                return
            if length <= 0:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "empty_body")
                return
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large")
                return

            body = self.rfile.read(length)
            try:
                submitted = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid_json")
                return

            try:
                events = validate_batch(submitted, maximum_events=MAX_BATCH_EVENTS)
            except EventValidationError as error:
                self._send_error_json(HTTPStatus.UNPROCESSABLE_ENTITY, error.code, error.path)
                return

            try:
                inserted = state.store.insert_events(events)
                state.maintain_retention()
            except Exception:
                LOGGER.exception("event storage failed")
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "storage_failure")
                return

            for event in events:
                state.broker.publish(event)
            self._send_json(HTTPStatus.ACCEPTED, {"accepted": len(events), "inserted": inserted})

    return Handler


def create_server(*, host: str, port: int, state: AppState) -> CollectorHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Phase 1 permits only the literal loopback host 127.0.0.1")
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    return CollectorHTTPServer((host, port), make_handler(state))
