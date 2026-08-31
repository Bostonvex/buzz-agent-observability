"""Loopback-only HTTP collector, query API, SSE feed, and static dashboard."""

from __future__ import annotations

import hmac
import io
import json
import logging
import mimetypes
import queue
import sys
import threading
import time
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from collector import __version__
from collector.schema import EventValidationError, validate_batch
from collector.storage import TelemetryStore

LOGGER = logging.getLogger("buzz_observability")
MAX_BODY_BYTES = 256 * 1024
MAX_BATCH_EVENTS = 100
MAX_QUERY_ROWS = 500
MAX_QUERY_OFFSET = 100_000
MAX_QUERY_RANGE_SECONDS = 180 * 24 * 60 * 60
FILTER_KEYS = {"agent": "agent_id", "harness": "harness", "model": "model", "endpoint": "endpoint_id", "outcome": "outcome"}


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
    provider_diagnostics: Callable[[], dict[str, Any]] | None = None

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

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _bounded_limit(query: dict[str, list[str]], default: int = 100) -> int:
    raw = query.get("limit", [str(default)])[0]
    try:
        return max(1, min(MAX_QUERY_ROWS, int(raw)))
    except ValueError:
        return default


def _bounded_offset(query: dict[str, list[str]]) -> int:
    try:
        return max(0, min(MAX_QUERY_OFFSET, int(query.get("offset", ["0"])[0])))
    except ValueError:
        return 0


def _parse_datetime(value: str, name: str) -> tuple[str, datetime]:
    if len(value) > 40:
        raise ValueError(f"invalid_{name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid_{name}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid_{name}")
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z"), utc


def _query_filters(query: dict[str, list[str]]) -> dict[str, str]:
    filters: dict[str, str] = {}
    parsed_dates: dict[str, datetime] = {}
    for name in ("since", "until"):
        value = query.get(name, [None])[0]
        if value:
            normalized, parsed = _parse_datetime(value, name)
            filters[name] = normalized
            parsed_dates[name] = parsed
    now = datetime.now(timezone.utc)
    if "since" not in parsed_dates and "until" not in parsed_dates:
        default_since = now - timedelta(days=30)
        filters["since"] = default_since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        parsed_dates["since"] = default_since
    elif "until" in parsed_dates and "since" not in parsed_dates:
        default_since = parsed_dates["until"] - timedelta(seconds=MAX_QUERY_RANGE_SECONDS)
        filters["since"] = default_since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        parsed_dates["since"] = default_since
    range_end = parsed_dates.get("until", now)
    if "since" in parsed_dates:
        seconds = (range_end - parsed_dates["since"]).total_seconds()
        if seconds < 0:
            raise ValueError("invalid_date_range")
        if seconds > MAX_QUERY_RANGE_SECONDS:
            raise ValueError("date_range_too_large")
    for query_name, storage_name in FILTER_KEYS.items():
        value = query.get(query_name, [None])[0]
        if value is None or value == "":
            continue
        if len(value) > 256 or any(ord(character) < 32 for character in value):
            raise ValueError(f"invalid_{query_name}")
        if query_name == "outcome" and value not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid_outcome")
        filters[storage_name] = value
    return filters


def _path_identifier(path: str, prefix: str, suffix: str = "") -> str | None:
    if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
        return None
    end = -len(suffix) if suffix else None
    value = unquote(path[len(prefix) : end])
    if not value or "/" in value or len(value) > 256 or any(ord(character) < 32 for character in value):
        return None
    return value


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

        def _send_bytes(self, status: int, content_type: str, body: bytes, **headers: str) -> None:
            self.send_response(status)
            self._headers(content_type, len(body))
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
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
            try:
                filters = _query_filters(query)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
                return
            if parsed.path in {"/", "/index.html"}:
                self._serve_asset("index.html")
            elif parsed.path == "/app.js":
                self._serve_asset("app.js")
            elif parsed.path == "/styles.css":
                self._serve_asset("styles.css")
            elif parsed.path == "/healthz":
                try:
                    health = state.store.health()
                    health["providers"] = state.provider_diagnostics() if state.provider_diagnostics else {}
                    self._send_json(HTTPStatus.OK, health)
                except Exception:
                    LOGGER.exception("health check failed")
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "unhealthy", "schema_version": 1})
            elif parsed.path == "/api/v1/agents":
                self._send_json(
                    HTTPStatus.OK,
                    {"agents": state.store.list_agents(limit=_bounded_limit(query), **filters)},
                )
            elif parsed.path == "/api/v1/turns":
                limit = _bounded_limit(query)
                offset = _bounded_offset(query)
                turns = state.store.list_turns(limit=limit, offset=offset, **filters)
                self._send_json(
                    HTTPStatus.OK,
                    {"turns": turns, "limit": limit, "offset": offset, "next_offset": offset + len(turns) if len(turns) == limit else None},
                )
            elif parsed.path == "/api/v1/summary":
                self._send_json(HTTPStatus.OK, state.store.summary(**filters))
            elif parsed.path == "/api/v1/samples":
                sample_filters = {
                    key: value for key, value in filters.items() if key in {"since", "until", "endpoint_id"}
                }
                self._send_json(
                    HTTPStatus.OK,
                    {"samples": state.store.list_samples(limit=_bounded_limit(query, 200), **sample_filters)},
                )
            elif (agent_id := _path_identifier(parsed.path, "/api/v1/agents/", "/summary")) is not None:
                summary = state.store.agent_summary(agent_id, **{key: value for key, value in filters.items() if key != "agent_id"})
                if summary is None:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "agent_not_found")
                else:
                    self._send_json(HTTPStatus.OK, summary)
            elif (turn_id := _path_identifier(parsed.path, "/api/v1/turns/")) is not None:
                detail = state.store.turn_detail(turn_id)
                if detail is None:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "turn_not_found")
                else:
                    self._send_json(HTTPStatus.OK, detail)
            elif parsed.path == "/api/v1/export.csv":
                turns = state.store.list_turns(limit=MAX_QUERY_ROWS, **filters)
                output = io.StringIO(newline="")
                fieldnames = [
                    "id", "agent_id", "agent_display_name", "harness", "model", "endpoint_id",
                    "started_at", "ended_at", "outcome", "ttfa_ms", "ttfvt_ms", "first_tool_ms",
                    "duration_ms", "max_stall_ms", "tool_count", "measurement_quality",
                    "error_category", "error_code",
                ]
                writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for turn in turns:
                    safe_turn = {
                        key: f"'{value}" if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value
                        for key, value in turn.items()
                    }
                    writer.writerow(safe_turn)
                self._send_bytes(
                    HTTPStatus.OK,
                    "text/csv; charset=utf-8",
                    output.getvalue().encode("utf-8"),
                    Content_Disposition='attachment; filename="buzz-agent-turns.csv"',
                )
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
        raise ValueError("the collector permits only the literal loopback host 127.0.0.1")
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    return CollectorHTTPServer((host, port), make_handler(state))
