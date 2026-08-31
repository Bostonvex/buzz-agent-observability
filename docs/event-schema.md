# Event contract version 1

Every event has a fixed envelope, a known event type, and an event-specific attribute allowlist. Unknown fields are rejected rather than silently stored. The machine-readable envelope is in [`config/event-schema-v1.json`](../config/event-schema-v1.json); `collector/schema.py` is the authoritative runtime validator.

Required envelope fields:

- `schema_version`, fixed to `1`.
- `event_id`, a UUID used for idempotent ingestion.
- `event_type`, one of the documented ACP, model, collector, server, or hardware events.
- `observed_at`, an ISO 8601 timestamp with an explicit timezone, normalized to UTC.
- `monotonic_offset_ms`, a non-negative producer-process clock offset.
- `producer`, containing only `name`, `version`, and random `instance_id`.
- `agent`, containing a privacy-preserving stable `id` and separate `display_name`.
- `harness`, `model`, `endpoint_id`, `session_id`, `turn_id`, `span_id`, and `parent_span_id`, each a safe identifier or `null`.
- `attributes`, restricted according to `event_type`.

Timing values carry `measurement_quality` when relevant: `exact`, `derived`, `estimated`, or `unavailable`. TTFA (`turn.first_activity`), TTFVT (`turn.first_visible_text`), and model TTFT (`model.first_token`) are distinct measurements and must not be relabeled.

Model events use one span per HTTP call. `model.completed` may carry exact
`connection_ms`, `first_byte_ms`, `decode_ms`, HTTP status, and documented
usage counters. `model.first_token.elapsed_ms` is measured from request start
to the first non-empty generated streaming delta. Non-streaming calls omit TTFT
and decode timing because a complete response cannot expose those boundaries.
`correlation` is always `exact`, `ambiguous`, or `unavailable`.

`monotonic_offset_ms` is comparable only within one producer process. The
dashboard aligns ACP and proxy events from different processes using
`observed_at`; durations remain based on each producer's monotonic clock.

The contract intentionally has no generic tags or arbitrary metadata map. Schema evolution adds specific reviewed attributes and increments the schema version when compatibility requires it.
