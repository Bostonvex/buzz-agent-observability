from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


def event(
    event_type: str = "turn.started",
    *,
    event_id: str | None = None,
    agent_id: str = "agent-alpha",
    display_name: str = "Agent Alpha",
    turn_id: str | None = "turn-alpha",
    attributes: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "observed_at": observed_at
        or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "monotonic_offset_ms": 10.5,
        "producer": {"name": "test-producer", "version": "0.1.0", "instance_id": "test-instance"},
        "agent": {"id": agent_id, "display_name": display_name},
        "harness": "deepseek",
        "model": "example-model",
        "endpoint_id": "local-example",
        "session_id": "hashed-session-alpha",
        "turn_id": turn_id,
        "span_id": None,
        "parent_span_id": None,
        "attributes": deepcopy(attributes or {}),
    }
