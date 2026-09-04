"""SQLite persistence for normalized metadata-only telemetry."""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


TERMINAL_OUTCOMES = {
    "turn.completed": "completed",
    "turn.failed": "failed",
    "turn.cancelled": "cancelled",
}

AGENT_STATES = {
    "process.started": "idle",
    "session.started": "idle",
    "turn.started": "waiting_for_activity",
    "turn.first_activity": "active",
    "turn.first_visible_text": "generating_text",
    "turn.first_tool": "running_tools",
    "turn.stall": "stalled",
    "tool.started": "running_tools",
    "tool.updated": "running_tools",
    "tool.completed": "active",
    "tool.failed": "active",
    "turn.completed": "completed",
    "turn.failed": "failed",
    "turn.cancelled": "cancelled",
    "session.ended": "idle",
    "process.exited": "offline",
}

CLEAR_TURN_EVENTS = {"process.started", "process.exited", "session.ended", *TERMINAL_OUTCOMES}

MIGRATION = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_display_name TEXT NOT NULL,
    harness TEXT,
    model TEXT,
    endpoint_id TEXT,
    session_id TEXT,
    turn_id TEXT,
    safe_payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_observed_at_idx ON events(observed_at);
CREATE INDEX IF NOT EXISTS events_agent_observed_idx ON events(agent_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS events_turn_observed_idx ON events(turn_id, observed_at);
CREATE INDEX IF NOT EXISTS events_dimensions_idx ON events(harness, model, endpoint_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    harness TEXT,
    model TEXT,
    endpoint_id TEXT,
    current_state TEXT NOT NULL,
    current_turn_id TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    ttfa_ms REAL,
    ttfvt_ms REAL,
    first_tool_ms REAL,
    duration_ms REAL,
    max_stall_ms REAL,
    tool_count INTEGER,
    tool_observation_mode TEXT,
    measurement_quality TEXT,
    error_category TEXT,
    error_code TEXT,
    cancellation_reason TEXT,
    harness TEXT,
    model TEXT,
    endpoint_id TEXT,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS turns_agent_started_idx ON turns(agent_id, started_at DESC);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TelemetryStore:
    def __init__(self, database: str | Path) -> None:
        self.path = Path(database).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.executescript(MIGRATION)
            self._migrate_turn_columns()
            self._repair_materialized_turns()
            self._repair_materialized_agents()
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (_utc_now(),),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (_utc_now(),),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, ?)",
                (_utc_now(),),
            )
            self._connection.commit()

    @staticmethod
    def _agent_display_name(display_name: str, harness: str | None) -> str:
        if not display_name.startswith("Unknown agent"):
            return display_name
        return {
            "deepseek": "DeepSeek",
            "qwen-code": "Qwen Code",
            "zcode": "ZCode",
        }.get(harness or "", "Agent")

    def _repair_materialized_agents(self) -> None:
        """Remove proxy-only pseudo-agents and normalize legacy fallback labels."""
        self._connection.execute(
            """
            DELETE FROM agents
            WHERE NOT EXISTS (SELECT 1 FROM turns WHERE turns.agent_id = agents.id)
              AND NOT EXISTS (
                  SELECT 1 FROM events
                  WHERE events.agent_id = agents.id
                    AND events.event_type NOT LIKE 'model.%'
              )
            """
        )
        for harness, display_name in (
            ("deepseek", "DeepSeek"),
            ("qwen-code", "Qwen Code"),
            ("zcode", "ZCode"),
        ):
            self._connection.execute(
                """
                UPDATE agents SET display_name = ?
                WHERE harness = ? AND display_name LIKE 'Unknown agent%'
                """,
                (display_name, harness),
            )

    def _migrate_turn_columns(self) -> None:
        existing = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(turns)").fetchall()
        }
        additions = {
            "first_tool_ms": "REAL",
            "measurement_quality": "TEXT",
            "error_category": "TEXT",
            "error_code": "TEXT",
            "cancellation_reason": "TEXT",
            "harness": "TEXT",
            "model": "TEXT",
            "endpoint_id": "TEXT",
            "tool_observation_mode": "TEXT",
        }
        for name, data_type in additions.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {data_type}")

    def _repair_materialized_turns(self) -> None:
        """Restore authoritative turn fields after older cross-scope event folding."""
        rows = self._connection.execute(
            """
            SELECT turn_id, event_type, observed_at, safe_payload_json
            FROM events
            WHERE turn_id IS NOT NULL
              AND event_type IN ('turn.completed', 'turn.failed', 'turn.cancelled')
            ORDER BY observed_at, rowid
            """
        ).fetchall()
        for row in rows:
            attributes = json.loads(row["safe_payload_json"])["attributes"]
            self._connection.execute(
                """
                UPDATE turns SET ended_at = ?, outcome = ?, duration_ms = ?,
                    max_stall_ms = ?, tool_count = ?, tool_observation_mode = ?, measurement_quality = ?,
                    error_category = ?, error_code = ?, cancellation_reason = ?
                WHERE id = ?
                """,
                (
                    row["observed_at"],
                    TERMINAL_OUTCOMES[row["event_type"]],
                    attributes.get("duration_ms"),
                    attributes.get("max_stall_ms"),
                    attributes.get("tool_count"),
                    attributes.get("tool_observation_mode"),
                    attributes.get("measurement_quality"),
                    attributes.get("error_category"),
                    attributes.get("error_code"),
                    attributes.get("cancellation_reason"),
                    row["turn_id"],
                ),
            )

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _upsert_agent(self, event: dict[str, Any]) -> None:
        existing = self._connection.execute(
            "SELECT last_seen_at, current_state, current_turn_id FROM agents WHERE id = ?",
            (event["agent"]["id"],),
        ).fetchone()
        if existing is not None and event["observed_at"] < existing["last_seen_at"]:
            return
        state = AGENT_STATES.get(event["event_type"], existing["current_state"] if existing else "active")
        if event["event_type"] in CLEAR_TURN_EVENTS:
            current_turn = None
        else:
            current_turn = event["turn_id"] or (existing["current_turn_id"] if existing else None)
        self._connection.execute(
            """
            INSERT INTO agents(
                id, display_name, first_seen_at, last_seen_at, harness, model,
                endpoint_id, current_state, current_turn_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen_at = excluded.last_seen_at,
                harness = COALESCE(excluded.harness, agents.harness),
                model = COALESCE(excluded.model, agents.model),
                endpoint_id = COALESCE(excluded.endpoint_id, agents.endpoint_id),
                current_state = excluded.current_state,
                current_turn_id = excluded.current_turn_id
            """,
            (
                event["agent"]["id"],
                self._agent_display_name(
                    event["agent"]["display_name"], event["harness"]
                ),
                event["observed_at"],
                event["observed_at"],
                event["harness"],
                event["model"],
                event["endpoint_id"],
                state,
                current_turn,
            ),
        )

    def _upsert_turn(self, event: dict[str, Any]) -> None:
        turn_id = event["turn_id"]
        if not turn_id:
            return
        attributes = event["attributes"]
        event_type = event["event_type"]
        terminal_turn = event_type in TERMINAL_OUTCOMES
        ended_at = event["observed_at"] if event_type in TERMINAL_OUTCOMES else None
        outcome = TERMINAL_OUTCOMES.get(event_type)
        self._connection.execute(
            """
            INSERT INTO turns(
                id, agent_id, session_id, started_at, ended_at, outcome, ttfa_ms,
                ttfvt_ms, first_tool_ms, duration_ms, max_stall_ms, tool_count,
                tool_observation_mode, measurement_quality, error_category, error_code, harness, model,
                cancellation_reason, endpoint_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                started_at = MIN(turns.started_at, excluded.started_at),
                ended_at = COALESCE(excluded.ended_at, turns.ended_at),
                outcome = COALESCE(excluded.outcome, turns.outcome),
                ttfa_ms = COALESCE(excluded.ttfa_ms, turns.ttfa_ms),
                ttfvt_ms = COALESCE(excluded.ttfvt_ms, turns.ttfvt_ms),
                first_tool_ms = COALESCE(excluded.first_tool_ms, turns.first_tool_ms),
                duration_ms = COALESCE(excluded.duration_ms, turns.duration_ms),
                max_stall_ms = CASE
                    WHEN excluded.max_stall_ms IS NULL THEN turns.max_stall_ms
                    WHEN turns.max_stall_ms IS NULL THEN excluded.max_stall_ms
                    ELSE MAX(turns.max_stall_ms, excluded.max_stall_ms)
                END,
                tool_count = COALESCE(excluded.tool_count, turns.tool_count),
                tool_observation_mode = COALESCE(excluded.tool_observation_mode, turns.tool_observation_mode),
                measurement_quality = COALESCE(excluded.measurement_quality, turns.measurement_quality),
                error_category = COALESCE(excluded.error_category, turns.error_category),
                error_code = COALESCE(excluded.error_code, turns.error_code),
                cancellation_reason = COALESCE(excluded.cancellation_reason, turns.cancellation_reason),
                harness = COALESCE(excluded.harness, turns.harness),
                model = COALESCE(excluded.model, turns.model),
                endpoint_id = COALESCE(excluded.endpoint_id, turns.endpoint_id)
            """,
            (
                turn_id,
                event["agent"]["id"],
                event["session_id"],
                event["observed_at"],
                ended_at,
                outcome,
                attributes.get("ttfa_ms")
                if "ttfa_ms" in attributes
                else attributes.get("elapsed_ms") if event_type == "turn.first_activity" else None,
                attributes.get("ttfvt_ms")
                if "ttfvt_ms" in attributes
                else attributes.get("elapsed_ms") if event_type == "turn.first_visible_text" else None,
                attributes.get("first_tool_ms")
                if "first_tool_ms" in attributes
                else attributes.get("elapsed_ms") if event_type == "turn.first_tool" else None,
                attributes.get("duration_ms") if terminal_turn else None,
                attributes.get("max_stall_ms", attributes.get("gap_ms") if event_type == "turn.stall" else None)
                if event_type.startswith("turn.") else None,
                attributes.get("tool_count") if terminal_turn else None,
                attributes.get("tool_observation_mode") if terminal_turn else None,
                attributes.get("measurement_quality") if event_type.startswith("turn.") else None,
                attributes.get("error_category") if event_type == "turn.failed" else None,
                attributes.get("error_code") if event_type == "turn.failed" else None,
                event["harness"],
                event["model"],
                attributes.get("cancellation_reason") if event_type == "turn.cancelled" else None,
                event["endpoint_id"],
            ),
        )

    def insert_events(self, events: Iterable[dict[str, Any]]) -> int:
        inserted = 0
        received_at = _utc_now()
        with self._lock, self._connection:
            for event in events:
                payload = json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO events(
                        event_id, schema_version, event_type, observed_at, received_at,
                        agent_id, agent_display_name, harness, model, endpoint_id,
                        session_id, turn_id, safe_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["schema_version"],
                        event["event_type"],
                        event["observed_at"],
                        received_at,
                        event["agent"]["id"],
                        event["agent"]["display_name"],
                        event["harness"],
                        event["model"],
                        event["endpoint_id"],
                        event["session_id"],
                        event["turn_id"],
                        payload,
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                    correlation = event["attributes"].get("correlation")
                    unattributed_model = (
                        event["event_type"].startswith("model.")
                        and correlation != "exact"
                    )
                    if (
                        event["event_type"] not in {"server.sample", "hardware.sample"}
                        and not unattributed_model
                    ):
                        self._upsert_agent(event)
                        self._upsert_turn(event)
        return inserted

    def list_samples(
        self,
        *,
        limit: int = 200,
        since: str | None = None,
        until: str | None = None,
        endpoint_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["event_type IN ('server.sample', 'hardware.sample')"]
        values: list[Any] = []
        if since is not None:
            clauses.append("observed_at >= ?")
            values.append(since)
        if until is not None:
            clauses.append("observed_at <= ?")
            values.append(until)
        if endpoint_id is not None:
            clauses.append("endpoint_id = ?")
            values.append(endpoint_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT event_type, observed_at, endpoint_id, safe_payload_json
                FROM events WHERE {' AND '.join(clauses)}
                ORDER BY observed_at DESC, rowid DESC LIMIT ?
                """,
                (*values, max(1, min(500, limit))),
            ).fetchall()
        samples = []
        for row in rows:
            event = json.loads(row["safe_payload_json"])
            samples.append(
                {
                    "event_type": row["event_type"],
                    "observed_at": row["observed_at"],
                    "endpoint_id": row["endpoint_id"],
                    "attributes": event["attributes"],
                    "scope": "shared_context",
                }
            )
        return samples

    @staticmethod
    def _filters(
        *,
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        endpoint_id: str | None = None,
        outcome: str | None = None,
        alias: str = "t",
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("agent_id", agent_id),
            ("harness", harness),
            ("model", model),
            ("endpoint_id", endpoint_id),
            ("outcome", outcome),
        ):
            if value is not None:
                clauses.append(f"{alias}.{column} = ?")
                values.append(value)
        if since is not None:
            clauses.append(f"{alias}.started_at >= ?")
            values.append(since)
        if until is not None:
            clauses.append(f"{alias}.started_at <= ?")
            values.append(until)
        return (" WHERE " + " AND ".join(clauses) if clauses else "", values)

    def list_agents(
        self,
        *,
        limit: int = 100,
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        endpoint_id: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("id", agent_id),
            ("harness", harness),
            ("model", model),
            ("endpoint_id", endpoint_id),
        ):
            if value is not None:
                clauses.append(f"a.{column} = ?")
                values.append(value)
        if since is not None:
            clauses.append("a.last_seen_at >= ?")
            values.append(since)
        if until is not None:
            clauses.append("a.first_seen_at <= ?")
            values.append(until)
        if outcome is not None:
            clauses.append("EXISTS (SELECT 1 FROM turns outcome_turn WHERE outcome_turn.agent_id = a.id AND outcome_turn.outcome = ?)")
            values.append(outcome)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, display_name, first_seen_at, last_seen_at, harness, model,
                       endpoint_id, current_state, current_turn_id,
                       (SELECT started_at FROM turns current_turn
                        WHERE current_turn.id = a.current_turn_id) AS current_turn_started_at
                FROM agents a {where} ORDER BY last_seen_at DESC LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_turns(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        endpoint_id: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        where, values = self._filters(
            since=since,
            until=until,
            agent_id=agent_id,
            harness=harness,
            model=model,
            endpoint_id=endpoint_id,
            outcome=outcome,
        )
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT t.id, t.agent_id, a.display_name AS agent_display_name, t.session_id,
                       t.started_at, t.ended_at, t.outcome, t.ttfa_ms, t.ttfvt_ms,
                       t.first_tool_ms, t.duration_ms, t.max_stall_ms, t.tool_count,
                       t.tool_observation_mode, t.measurement_quality, t.error_category, t.error_code,
                       t.cancellation_reason, t.harness, t.model, t.endpoint_id
                FROM turns t JOIN agents a ON a.id = t.agent_id
                {where} ORDER BY t.started_at DESC LIMIT ? OFFSET ?
                """,
                (*values, limit, max(0, offset)),
            ).fetchall()
        turns = [dict(row) for row in rows]
        model_events: dict[str, list[dict[str, Any]]] = {
            str(turn["id"]): [] for turn in turns
        }
        if model_events:
            placeholders = ",".join("?" for _ in model_events)
            with self._lock:
                event_rows = self._connection.execute(
                    f"""
                    SELECT turn_id, safe_payload_json FROM events
                    WHERE turn_id IN ({placeholders}) AND event_type LIKE 'model.%'
                    ORDER BY observed_at, rowid
                    """,
                    tuple(model_events),
                ).fetchall()
            for event_row in event_rows:
                model_events[str(event_row["turn_id"])].append(
                    json.loads(event_row["safe_payload_json"])
                )
        for turn in turns:
            metrics = self._model_metrics(model_events[str(turn["id"])])
            turn["output_tokens_per_second"] = metrics["output_tokens_per_second"]
        return turns

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * percentile
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    @classmethod
    def _numeric_summary(cls, values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "mean": mean(values) if values else None,
            "p05": cls._percentile(values, 0.05),
            "p50": cls._percentile(values, 0.50),
            "p95": cls._percentile(values, 0.95),
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
        }

    @classmethod
    def _decode_concurrency_bands(
        cls, exact_decode_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        intervals: list[dict[str, Any]] = []
        for event in exact_decode_events:
            attributes = event["attributes"]
            decode_ms = float(attributes["decode_ms"])
            try:
                completed_at = datetime.fromisoformat(
                    event["observed_at"].replace("Z", "+00:00")
                ).timestamp() * 1000
            except (KeyError, TypeError, ValueError):
                continue
            intervals.append(
                {
                    "endpoint": event.get("endpoint_id") or "unknown",
                    "start_ms": completed_at - decode_ms,
                    "end_ms": completed_at,
                    "midpoint_ms": completed_at - decode_ms / 2,
                    "decode_ms": decode_ms,
                    "output_tokens": float(attributes["output_tokens"]),
                    "tokens_per_second": float(attributes["output_tokens"])
                    / (decode_ms / 1000),
                }
            )

        endpoint_intervals: dict[str, list[dict[str, Any]]] = {}
        for interval in intervals:
            endpoint_intervals.setdefault(str(interval["endpoint"]), []).append(interval)
        for endpoint_group in endpoint_intervals.values():
            starts = sorted(endpoint_group, key=lambda interval: interval["start_ms"])
            midpoint_order = sorted(
                endpoint_group, key=lambda interval: interval["midpoint_ms"]
            )
            active_ends: list[float] = []
            start_index = 0
            for interval in midpoint_order:
                midpoint = interval["midpoint_ms"]
                while (
                    start_index < len(starts)
                    and starts[start_index]["start_ms"] <= midpoint
                ):
                    heapq.heappush(active_ends, starts[start_index]["end_ms"])
                    start_index += 1
                while active_ends and active_ends[0] < midpoint:
                    heapq.heappop(active_ends)
                interval["concurrency"] = float(len(active_ends))

        definitions = (
            ("1", 1, 1),
            ("2", 2, 2),
            ("3-4", 3, 4),
            ("5-8", 5, 8),
            ("9+", 9, None),
        )
        bands = []
        for label, minimum, maximum in definitions:
            matching = [
                interval
                for interval in intervals
                if interval["concurrency"] >= minimum
                and (maximum is None or interval["concurrency"] <= maximum)
            ]
            decode_ms = sum(interval["decode_ms"] for interval in matching)
            output_tokens = sum(interval["output_tokens"] for interval in matching)
            per_call_rates = [interval["tokens_per_second"] for interval in matching]
            bands.append(
                {
                    "band": label,
                    "call_count": len(matching),
                    "average_concurrency": mean(
                        [interval["concurrency"] for interval in matching]
                    )
                    if matching
                    else None,
                    "output_tokens_per_second": output_tokens / (decode_ms / 1000)
                    if decode_ms > 0
                    else None,
                    "per_call_p50_tokens_per_second": cls._percentile(per_call_rates, 0.50),
                }
            )
        return bands

    @classmethod
    def _metric(cls, rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
        values = [float(row[name]) for row in rows if row.get(name) is not None]
        qualities: dict[str, int] = {}
        for row in rows:
            if row.get(name) is None:
                continue
            quality = row.get("measurement_quality") or "unavailable"
            qualities[quality] = qualities.get(quality, 0) + 1
        return {
            "count": len(values),
            "mean": mean(values) if values else None,
            "median": median(values) if values else None,
            "p50": cls._percentile(values, 0.50),
            "p95": cls._percentile(values, 0.95),
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "quality_counts": qualities,
        }

    @classmethod
    def _aggregate(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        outcomes: dict[str, int] = {"completed": 0, "failed": 0, "cancelled": 0, "active": 0}
        cancellation_reasons: dict[str, int] = {}
        for row in rows:
            outcome = row.get("outcome") or "active"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == "cancelled":
                reason = row.get("cancellation_reason") or "unavailable"
                cancellation_reasons[reason] = cancellation_reasons.get(reason, 0) + 1
        terminal = sum(value for key, value in outcomes.items() if key != "active")
        tool_observed = [
            row for row in rows
            if row.get("outcome") and row.get("tool_observation_mode") not in (None, "unavailable")
        ]
        tool_unavailable = [
            row for row in rows
            if row.get("outcome") and row.get("tool_observation_mode") in (None, "unavailable")
        ]
        return {
            "turn_count": len(rows),
            "active_turns": outcomes.get("active", 0),
            "outcomes": outcomes,
            "cancellation_reasons": cancellation_reasons,
            "success_rate": outcomes.get("completed", 0) / terminal if terminal else None,
            "failure_rate": outcomes.get("failed", 0) / terminal if terminal else None,
            "cancellation_rate": outcomes.get("cancelled", 0) / terminal if terminal else None,
            "tool_observation": {
                "observed_turns": len(tool_observed),
                "unavailable_turns": len(tool_unavailable),
                "coverage": len(tool_observed) / terminal if terminal else None,
                "tool_uses": sum(int(row.get("tool_count") or 0) for row in tool_observed),
                "turns_with_tools": sum(int(row.get("tool_count") or 0) > 0 for row in tool_observed),
            },
            "metrics": {
                name: cls._metric(rows, name)
                for name in ("ttfa_ms", "ttfvt_ms", "first_tool_ms", "duration_ms", "max_stall_ms")
            },
        }

    @classmethod
    def _model_metrics(cls, model_events: list[dict[str, Any]]) -> dict[str, Any]:
        terminal_events = [
            event
            for event in model_events
            if event["event_type"] in {"model.completed", "model.failed"}
        ]
        ttft_values = [
            float(event["attributes"]["elapsed_ms"])
            for event in model_events
            if event["event_type"] == "model.first_token"
            and isinstance(event["attributes"].get("elapsed_ms"), (int, float))
        ]
        exact_decode_events = [
            event
            for event in terminal_events
            if event["event_type"] == "model.completed"
            and event["attributes"].get("measurement_quality") == "exact"
            and isinstance(event["attributes"].get("decode_ms"), (int, float))
            and event["attributes"].get("decode_ms", 0) > 0
            and isinstance(event["attributes"].get("output_tokens"), int)
        ]
        decode_ms = sum(
            float(event["attributes"]["decode_ms"]) for event in exact_decode_events
        )
        output_tokens = sum(
            int(event["attributes"]["output_tokens"]) for event in exact_decode_events
        )
        input_token_values = [
            float(event["attributes"]["input_tokens"])
            for event in terminal_events
            if event["event_type"] == "model.completed"
            and event["attributes"].get("measurement_quality") == "exact"
            and isinstance(event["attributes"].get("input_tokens"), int)
        ]
        per_call_rates = [
            float(event["attributes"]["output_tokens"])
            / (float(event["attributes"]["decode_ms"]) / 1000)
            for event in exact_decode_events
        ]
        cached_token_values = [
            float(event["attributes"]["cached_tokens"])
            for event in terminal_events
            if event["event_type"] == "model.completed"
            and isinstance(event["attributes"].get("cached_tokens"), int)
        ]
        reasoning_token_values = [
            float(event["attributes"]["reasoning_tokens"])
            for event in terminal_events
            if event["event_type"] == "model.completed"
            and isinstance(event["attributes"].get("reasoning_tokens"), int)
        ]
        cached_token_sum = sum(cached_token_values) if cached_token_values else None
        reasoning_token_sum = (
            sum(reasoning_token_values) if reasoning_token_values else None
        )
        correlations: dict[str, int] = {}
        for event in terminal_events:
            correlation = str(event["attributes"].get("correlation", "unavailable"))
            correlations[correlation] = correlations.get(correlation, 0) + 1
        return {
            "call_count": sum(
                event["event_type"] == "model.request_started" for event in model_events
            ),
            "completed_count": sum(
                event["event_type"] == "model.completed" for event in model_events
            ),
            "failed_count": sum(
                event["event_type"] == "model.failed" for event in model_events
            ),
            "exact_call_count": len(exact_decode_events),
            "attributed_exact_call_count": sum(
                event["attributes"].get("correlation") == "exact"
                for event in exact_decode_events
            ),
            "ttft_ms": {
                "count": len(ttft_values),
                "p50": cls._percentile(ttft_values, 0.50),
                "p95": cls._percentile(ttft_values, 0.95),
                "minimum": min(ttft_values) if ttft_values else None,
                "maximum": max(ttft_values) if ttft_values else None,
            },
            "input_tokens": cls._numeric_summary(input_token_values),
            "per_call_output_tokens_per_second": cls._numeric_summary(per_call_rates),
            "decode_concurrency_bands": cls._decode_concurrency_bands(exact_decode_events),
            "exact_output_tokens": output_tokens,
            "exact_decode_ms": decode_ms or None,
            "output_tokens_per_second": output_tokens / (decode_ms / 1000)
            if decode_ms > 0
            else None,
            "cached_tokens": {"count": len(cached_token_values), "sum": cached_token_sum},
            "reasoning_tokens": {
                "count": len(reasoning_token_values),
                "sum": reasoning_token_sum,
            },
            "correlation_counts": correlations,
        }

    def _model_events_for_turns(self, turn_ids: list[str]) -> list[dict[str, Any]]:
        if not turn_ids:
            return []
        placeholders = ",".join("?" for _ in turn_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT safe_payload_json FROM events
                WHERE turn_id IN ({placeholders}) AND event_type LIKE 'model.%'
                ORDER BY observed_at, rowid
                """,
                turn_ids,
            ).fetchall()
        return [json.loads(row["safe_payload_json"]) for row in rows]

    def _filtered_model_events(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        endpoint_id: str | None = None,
        outcome: str | None = None,
        limit: int = 20_000,
    ) -> tuple[list[dict[str, Any]], bool]:
        clauses = ["e.event_type LIKE 'model.%'"]
        values: list[Any] = []
        for column, value in (
            ("agent_id", agent_id),
            ("harness", harness),
            ("model", model),
            ("endpoint_id", endpoint_id),
        ):
            if value is not None:
                clauses.append(f"e.{column} = ?")
                values.append(value)
        if since is not None:
            clauses.append("e.observed_at >= ?")
            values.append(since)
        if until is not None:
            clauses.append("e.observed_at <= ?")
            values.append(until)
        if outcome is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM turns model_turn "
                "WHERE model_turn.id = e.turn_id AND model_turn.outcome = ?)"
            )
            values.append(outcome)
        bounded = max(1, min(50_000, limit))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT e.safe_payload_json FROM events e
                WHERE {' AND '.join(clauses)}
                ORDER BY e.observed_at DESC, e.rowid DESC LIMIT ?
                """,
                (*values, bounded + 1),
            ).fetchall()
        limited = len(rows) > bounded
        return (
            [json.loads(row["safe_payload_json"]) for row in rows[:bounded]],
            limited,
        )

    def _infrastructure_metrics(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["event_type IN ('server.sample', 'hardware.sample')"]
        values: list[Any] = []
        if since is not None:
            clauses.append("observed_at >= ?")
            values.append(since)
        if until is not None:
            clauses.append("observed_at <= ?")
            values.append(until)
        if endpoint_id is not None:
            clauses.append("endpoint_id = ?")
            values.append(endpoint_id)

        with self._lock:
            rows = self._connection.execute(
                f"""
                WITH samples AS (
                    SELECT rowid, observed_at, event_type, endpoint_id,
                           json_extract(safe_payload_json, '$.attributes.provider_id') AS provider_id,
                           json_extract(safe_payload_json, '$.attributes.node_id') AS node_id,
                           json_extract(safe_payload_json, '$.attributes.metric_name') AS metric_name,
                           json_extract(safe_payload_json, '$.attributes.unit') AS unit,
                           json_extract(safe_payload_json, '$.attributes.measurement_quality') AS measurement_quality,
                           CAST(json_extract(safe_payload_json, '$.attributes.value') AS REAL) AS value,
                           (julianday(observed_at) - 2440587.5) * 86400.0 AS observed_seconds
                    FROM events WHERE {' AND '.join(clauses)}
                ), ordered AS (
                    SELECT *,
                           lag(value) OVER series_order AS previous_value,
                           row_number() OVER series_latest AS latest_rank
                    FROM samples
                    WINDOW
                        series_order AS (
                            PARTITION BY event_type, endpoint_id, provider_id, node_id, metric_name, unit
                            ORDER BY observed_at, rowid
                        ),
                        series_latest AS (
                            PARTITION BY event_type, endpoint_id, provider_id, node_id, metric_name, unit
                            ORDER BY observed_at DESC, rowid DESC
                        )
                )
                SELECT event_type, endpoint_id, provider_id, node_id, metric_name, unit,
                       COUNT(*) AS sample_count, AVG(value) AS mean_value,
                       MIN(value) AS minimum_value, MAX(value) AS maximum_value,
                       MAX(CASE WHEN latest_rank = 1 THEN value END) AS latest_value,
                       MAX(CASE WHEN latest_rank = 1 THEN observed_at END) AS latest_at,
                       MAX(CASE WHEN latest_rank = 1 THEN measurement_quality END) AS measurement_quality,
                       SUM(CASE
                               WHEN previous_value IS NULL THEN 0
                               WHEN value >= previous_value THEN value - previous_value
                               ELSE 0
                           END) AS positive_delta,
                       SUM(CASE WHEN previous_value IS NOT NULL AND value < previous_value THEN 1 ELSE 0 END)
                           AS counter_resets,
                       MAX(observed_seconds) - MIN(observed_seconds) AS elapsed_seconds
                FROM ordered
                GROUP BY event_type, endpoint_id, provider_id, node_id, metric_name, unit
                ORDER BY event_type, endpoint_id, provider_id, node_id, metric_name
                """,
                tuple(values),
            ).fetchall()

        counter_metrics = {
            "prompt_tokens_total",
            "generation_tokens_total",
            "successful_requests_total",
            "preemptions_total",
        }
        series = []
        for row in rows:
            elapsed_seconds = float(row["elapsed_seconds"] or 0)
            metric_name = str(row["metric_name"])
            rate = None
            if metric_name in counter_metrics and elapsed_seconds > 0:
                rate = float(row["positive_delta"] or 0) / elapsed_seconds
            series.append(
                {
                    "scope": "server" if row["event_type"] == "server.sample" else "hardware",
                    "endpoint_id": row["endpoint_id"],
                    "provider_id": row["provider_id"],
                    "node_id": row["node_id"],
                    "metric_name": metric_name,
                    "unit": row["unit"],
                    "sample_count": int(row["sample_count"]),
                    "mean": float(row["mean_value"]),
                    "minimum": float(row["minimum_value"]),
                    "maximum": float(row["maximum_value"]),
                    "latest": float(row["latest_value"]),
                    "latest_at": row["latest_at"],
                    "measurement_quality": row["measurement_quality"] or "unavailable",
                    "rate_per_second": rate,
                    "counter_resets": int(row["counter_resets"] or 0)
                    if metric_name in counter_metrics
                    else None,
                }
            )

        generation_rates = [
            item["rate_per_second"]
            for item in series
            if item["metric_name"] == "generation_tokens_total"
            and item["rate_per_second"] is not None
        ]
        return {
            "series": series,
            "sample_count": sum(item["sample_count"] for item in series),
            "generation_tokens_per_second": sum(generation_rates) if generation_rates else None,
        }

    def summary(self, **filters: Any) -> dict[str, Any]:
        rows = self.list_turns(limit=500, **filters)
        agents = self.list_agents(limit=500, **filters)

        def groups(key: str) -> list[dict[str, Any]]:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                label = row.get(key) or "unknown"
                grouped.setdefault(label, []).append(row)
            return [
                {"value": label, **self._aggregate(group_rows)}
                for label, group_rows in sorted(grouped.items())
            ]

        dimensions: dict[str, list[str]] = {}
        with self._lock:
            for response_key, column in (
                ("agents", "agent_id"),
                ("harnesses", "harness"),
                ("models", "model"),
                ("endpoints", "endpoint_id"),
                ("outcomes", "outcome"),
            ):
                found = self._connection.execute(
                    f"SELECT DISTINCT {column} FROM turns WHERE {column} IS NOT NULL ORDER BY {column} LIMIT 500"
                ).fetchall()
                dimensions[response_key] = [str(row[0]) for row in found]
        fleet = {
            "active_agents": sum(agent["current_turn_id"] is not None for agent in agents),
            **self._aggregate(rows),
        }
        model_events, model_events_limited = self._filtered_model_events(**filters)
        fleet["model_metrics"] = self._model_metrics(model_events)
        fleet["model_metrics"]["limited"] = model_events_limited
        infrastructure_filters = {
            key: value
            for key, value in filters.items()
            if key in {"since", "until", "endpoint_id"}
        }
        fleet["infrastructure_metrics"] = self._infrastructure_metrics(
            **infrastructure_filters
        )
        return {
            "fleet": fleet,
            "groups": {
                "agents": groups("agent_id"),
                "harnesses": groups("harness"),
                "models": groups("model"),
                "endpoints": groups("endpoint_id"),
            },
            "dimensions": dimensions,
            "limited": len(rows) == 500,
        }

    def agent_summary(self, agent_id: str, **filters: Any) -> dict[str, Any] | None:
        agents = self.list_agents(limit=1, agent_id=agent_id)
        if not agents:
            return None
        rows = self.list_turns(limit=100, agent_id=agent_id, **filters)
        return {"agent": agents[0], "aggregate": self._aggregate(rows), "turns": rows}

    def turn_detail(self, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT t.*, a.display_name AS agent_display_name
                FROM turns t JOIN agents a ON a.id = t.agent_id WHERE t.id = ?
                """,
                (turn_id,),
            ).fetchone()
            if row is None:
                return None
            event_rows = self._connection.execute(
                """
                SELECT event_type, observed_at, safe_payload_json
                FROM events WHERE turn_id = ? ORDER BY observed_at, rowid LIMIT 500
                """,
                (turn_id,),
            ).fetchall()
            terminal = row["ended_at"] or _utc_now()
            shared_rows = self._connection.execute(
                """
                SELECT event_type, observed_at, safe_payload_json
                FROM events
                WHERE event_type IN ('server.sample', 'hardware.sample')
                  AND observed_at >= ? AND observed_at <= ?
                ORDER BY observed_at, rowid LIMIT 200
                """,
                (row["started_at"], terminal),
            ).fetchall()

        turn_origin = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))

        def timeline(items: Iterable[sqlite3.Row], *, shared: bool) -> list[dict[str, Any]]:
            result = []
            for item in items:
                event = json.loads(item["safe_payload_json"])
                event_time = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                result.append(
                    {
                        "event_type": item["event_type"],
                        "observed_at": item["observed_at"],
                        "monotonic_offset_ms": event["monotonic_offset_ms"],
                        "relative_ms": max(0.0, (event_time - turn_origin).total_seconds() * 1000),
                        "span_id": event["span_id"],
                        "parent_span_id": event["parent_span_id"],
                        "attributes": event["attributes"],
                        "scope": "shared_context" if shared else "turn",
                    }
                )
            return result

        model_events = [
            json.loads(item["safe_payload_json"])
            for item in event_rows
            if item["event_type"].startswith("model.")
        ]
        model_metrics = self._model_metrics(model_events)

        return {
            "turn": dict(row),
            "timeline": timeline(event_rows, shared=False),
            "shared_context": timeline(shared_rows, shared=True),
            "model_metrics": model_metrics,
        }

    def purge_expired_raw(self, *, retention_days: int = 7, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = (current - timedelta(days=retention_days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM events WHERE observed_at < ?", (cutoff,))
        return cursor.rowcount

    def purge_raw_before(self, before: datetime) -> int:
        if before.tzinfo is None:
            raise ValueError("purge cutoff must include a timezone")
        cutoff = before.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM events WHERE observed_at < ?", (cutoff,))
        return cursor.rowcount

    def backup_to(self, destination: str | Path) -> Path:
        target = Path(destination).expanduser()
        if target.exists():
            raise FileExistsError("backup destination already exists")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = sqlite3.connect(target)
        try:
            with self._lock:
                self._connection.backup(backup)
        except Exception:
            backup.close()
            target.unlink(missing_ok=True)
            raise
        finally:
            try:
                backup.close()
            except sqlite3.Error:
                pass
        os.chmod(target, 0o600)
        return target

    def health(self) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("SELECT 1").fetchone()
            event_count = int(self._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            agent_count = int(self._connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0])
            turn_count = int(self._connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
        return {
            "status": "ok",
            "schema_version": 1,
            "journal_mode": self.journal_mode,
            "events": event_count,
            "agents": agent_count,
            "turns": turn_count,
        }
