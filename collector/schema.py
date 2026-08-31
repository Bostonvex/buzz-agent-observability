"""Strict validation for the metadata-only telemetry event contract."""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

EVENT_TYPES = frozenset(
    {
        "process.started",
        "process.exited",
        "session.started",
        "session.ended",
        "turn.started",
        "turn.first_activity",
        "turn.first_visible_text",
        "turn.first_tool",
        "turn.stall",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "tool.started",
        "tool.updated",
        "tool.completed",
        "tool.failed",
        "usage.updated",
        "model.request_started",
        "model.first_token",
        "model.completed",
        "model.failed",
        "collector.dropped_events",
        "protocol.anomaly",
        "server.sample",
        "hardware.sample",
    }
)

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "observed_at",
        "monotonic_offset_ms",
        "producer",
        "agent",
        "harness",
        "model",
        "endpoint_id",
        "session_id",
        "turn_id",
        "span_id",
        "parent_span_id",
        "attributes",
    }
)

PRODUCER_FIELDS = frozenset({"name", "version", "instance_id"})
AGENT_FIELDS = frozenset({"id", "display_name"})

COMMON_ATTRIBUTES = frozenset({"measurement_quality"})
EVENT_ATTRIBUTES = {
    "process.started": frozenset({"harness_version"}),
    "process.exited": frozenset({"exit_code", "signal", "outcome"}),
    "session.started": frozenset(),
    "session.ended": frozenset({"duration_ms", "outcome"}),
    "turn.started": frozenset({"turn_class", "temperature_profile"}),
    "turn.first_activity": frozenset({"elapsed_ms", "update_kind"}),
    "turn.first_visible_text": frozenset({"elapsed_ms"}),
    "turn.first_tool": frozenset({"elapsed_ms", "tool_kind"}),
    "turn.stall": frozenset({"elapsed_ms", "gap_ms", "threshold_ms"}),
    "turn.completed": frozenset(
        {
            "duration_ms",
            "ttfa_ms",
            "ttfvt_ms",
            "first_tool_ms",
            "max_stall_ms",
            "tool_count",
            "outcome",
        }
    ),
    "turn.failed": frozenset(
        {
            "duration_ms",
            "ttfa_ms",
            "ttfvt_ms",
            "first_tool_ms",
            "max_stall_ms",
            "tool_count",
            "error_category",
            "error_code",
        }
    ),
    "turn.cancelled": frozenset(
        {"duration_ms", "ttfa_ms", "ttfvt_ms", "first_tool_ms", "max_stall_ms", "tool_count"}
    ),
    "tool.started": frozenset({"tool_kind", "status"}),
    "tool.updated": frozenset({"tool_kind", "status", "elapsed_ms"}),
    "tool.completed": frozenset({"tool_kind", "status", "duration_ms"}),
    "tool.failed": frozenset({"tool_kind", "status", "duration_ms", "error_category", "error_code"}),
    "usage.updated": frozenset({"token_kind", "value", "semantics"}),
    "model.request_started": frozenset({"correlation"}),
    "model.first_token": frozenset({"elapsed_ms", "correlation"}),
    "model.completed": frozenset(
        {
            "duration_ms",
            "connection_ms",
            "first_byte_ms",
            "decode_ms",
            "http_status",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "correlation",
        }
    ),
    "model.failed": frozenset(
        {
            "duration_ms",
            "connection_ms",
            "first_byte_ms",
            "http_status",
            "error_category",
            "error_code",
            "correlation",
        }
    ),
    "collector.dropped_events": frozenset({"dropped_count", "queue_depth"}),
    "protocol.anomaly": frozenset({"anomaly_kind", "line_bytes"}),
    "server.sample": frozenset({"metric_name", "value", "unit"}),
    "hardware.sample": frozenset({"provider_id", "node_id", "metric_name", "value", "unit"}),
}

QUALITY_VALUES = frozenset({"exact", "derived", "estimated", "unavailable"})
CORRELATION_VALUES = frozenset({"exact", "ambiguous", "unavailable"})

NUMERIC_ATTRIBUTES = frozenset(
    {
        "elapsed_ms",
        "gap_ms",
        "threshold_ms",
        "duration_ms",
        "connection_ms",
        "first_byte_ms",
        "decode_ms",
        "ttfa_ms",
        "ttfvt_ms",
        "first_tool_ms",
        "max_stall_ms",
        "value",
    }
)
INTEGER_ATTRIBUTES = frozenset(
    {
        "exit_code",
        "tool_count",
        "http_status",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "dropped_count",
        "queue_depth",
        "line_bytes",
    }
)
STRING_ATTRIBUTES = frozenset(
    {
        "harness_version",
        "signal",
        "outcome",
        "turn_class",
        "temperature_profile",
        "update_kind",
        "tool_kind",
        "status",
        "error_category",
        "error_code",
        "token_kind",
        "semantics",
        "anomaly_kind",
        "metric_name",
        "unit",
        "provider_id",
        "node_id",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@=-]{0,255}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_PATTERNS = (
    re.compile("AK" + r"IA[0-9A-Z]{16}"),
    re.compile("gh" + r"[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile("s" + r"k-[A-Za-z0-9_-]{20,}"),
    re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class EventValidationError(ValueError):
    """A safe validation error that never echoes submitted values."""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code} at {path}")


def _fail(code: str, path: str) -> None:
    raise EventValidationError(code, path)


def _require_exact_fields(value: Any, allowed: frozenset[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("expected_object", path)
    unknown = set(value) - allowed
    if unknown:
        _fail("unknown_field", f"{path}.{sorted(unknown)[0]}")
    missing = allowed - set(value)
    if missing:
        _fail("missing_field", f"{path}.{sorted(missing)[0]}")
    return value


def _safe_string(value: Any, path: str, *, maximum: int = 256, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail("invalid_string", path)
    if _CONTROL.search(value) or "\n" in value or "\r" in value:
        _fail("unsafe_string", path)
    if identifier and not _IDENTIFIER.fullmatch(value):
        _fail("invalid_identifier", path)
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        _fail("secret_like_value", path)
    return value


def _nullable_identifier(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _safe_string(value, path, identifier=True)


def _number(value: Any, path: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        _fail("invalid_number", path)
    if integer:
        if not isinstance(value, int) or value < 0 or value > 10**12:
            _fail("invalid_integer", path)
        return value
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > 10**15:
        _fail("invalid_number", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    raw = _safe_string(value, path, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        _fail("invalid_timestamp", path)
    if parsed.tzinfo is None:
        _fail("timestamp_requires_timezone", path)
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _attributes(event_type: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("expected_object", "$.attributes")
    allowed = EVENT_ATTRIBUTES[event_type] | COMMON_ATTRIBUTES
    unknown = set(value) - allowed
    if unknown:
        _fail("unknown_attribute", f"$.attributes.{sorted(unknown)[0]}")
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"$.attributes.{key}"
        if key in NUMERIC_ATTRIBUTES:
            result[key] = _number(item, path)
        elif key in INTEGER_ATTRIBUTES:
            result[key] = _number(item, path, integer=True)
        elif key == "measurement_quality":
            if item not in QUALITY_VALUES:
                _fail("invalid_measurement_quality", path)
            result[key] = item
        elif key == "correlation":
            if item not in CORRELATION_VALUES:
                _fail("invalid_correlation", path)
            result[key] = item
        elif key in STRING_ATTRIBUTES:
            result[key] = _safe_string(item, path, maximum=128, identifier=False)
        else:  # Defensive: the allowlist and type table must evolve together.
            _fail("unsupported_attribute", path)
    return result


def validate_event(value: Any) -> dict[str, Any]:
    """Validate and return a normalized, storage-safe event."""

    event = _require_exact_fields(value, TOP_LEVEL_FIELDS, "$")
    if event["schema_version"] != SCHEMA_VERSION:
        _fail("unsupported_schema_version", "$.schema_version")

    try:
        event_id = str(uuid.UUID(_safe_string(event["event_id"], "$.event_id", maximum=36)))
    except ValueError:
        _fail("invalid_uuid", "$.event_id")

    event_type = _safe_string(event["event_type"], "$.event_type", maximum=64, identifier=True)
    if event_type not in EVENT_TYPES:
        _fail("unsupported_event_type", "$.event_type")

    producer = _require_exact_fields(event["producer"], PRODUCER_FIELDS, "$.producer")
    normalized_producer = {
        "name": _safe_string(producer["name"], "$.producer.name", maximum=128, identifier=True),
        "version": _safe_string(producer["version"], "$.producer.version", maximum=64, identifier=True),
        "instance_id": _safe_string(producer["instance_id"], "$.producer.instance_id", maximum=128, identifier=True),
    }

    agent = _require_exact_fields(event["agent"], AGENT_FIELDS, "$.agent")
    normalized_agent = {
        "id": _safe_string(agent["id"], "$.agent.id", maximum=128, identifier=True),
        "display_name": _safe_string(agent["display_name"], "$.agent.display_name", maximum=128),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "observed_at": _timestamp(event["observed_at"], "$.observed_at"),
        "monotonic_offset_ms": _number(event["monotonic_offset_ms"], "$.monotonic_offset_ms"),
        "producer": normalized_producer,
        "agent": normalized_agent,
        "harness": _nullable_identifier(event["harness"], "$.harness"),
        "model": _nullable_identifier(event["model"], "$.model"),
        "endpoint_id": _nullable_identifier(event["endpoint_id"], "$.endpoint_id"),
        "session_id": _nullable_identifier(event["session_id"], "$.session_id"),
        "turn_id": _nullable_identifier(event["turn_id"], "$.turn_id"),
        "span_id": _nullable_identifier(event["span_id"], "$.span_id"),
        "parent_span_id": _nullable_identifier(event["parent_span_id"], "$.parent_span_id"),
        "attributes": _attributes(event_type, event["attributes"]),
    }


def validate_batch(value: Any, *, maximum_events: int = 100) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if not value:
            _fail("empty_batch", "$")
        if len(value) > maximum_events:
            _fail("batch_too_large", "$")
        return [validate_event(item) for item in value]
    return [validate_event(value)]
