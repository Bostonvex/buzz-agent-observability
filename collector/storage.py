"""SQLite persistence for normalized metadata-only telemetry."""

from __future__ import annotations

import json
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
    measurement_quality TEXT,
    error_category TEXT,
    error_code TEXT,
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
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (_utc_now(),),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (_utc_now(),),
            )
            self._connection.commit()

    def _migrate_turn_columns(self) -> None:
        existing = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(turns)").fetchall()
        }
        additions = {
            "first_tool_ms": "REAL",
            "measurement_quality": "TEXT",
            "error_category": "TEXT",
            "error_code": "TEXT",
            "harness": "TEXT",
            "model": "TEXT",
            "endpoint_id": "TEXT",
        }
        for name, data_type in additions.items():
            if name not in existing:
                self._connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {data_type}")

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
                event["agent"]["display_name"],
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
        ended_at = event["observed_at"] if event_type in TERMINAL_OUTCOMES else None
        outcome = TERMINAL_OUTCOMES.get(event_type)
        self._connection.execute(
            """
            INSERT INTO turns(
                id, agent_id, session_id, started_at, ended_at, outcome, ttfa_ms,
                ttfvt_ms, first_tool_ms, duration_ms, max_stall_ms, tool_count,
                measurement_quality, error_category, error_code, harness, model,
                endpoint_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                measurement_quality = COALESCE(excluded.measurement_quality, turns.measurement_quality),
                error_category = COALESCE(excluded.error_category, turns.error_category),
                error_code = COALESCE(excluded.error_code, turns.error_code),
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
                attributes.get("duration_ms"),
                attributes.get("max_stall_ms", attributes.get("gap_ms") if event_type == "turn.stall" else None),
                attributes.get("tool_count"),
                attributes.get("measurement_quality"),
                attributes.get("error_category"),
                attributes.get("error_code"),
                event["harness"],
                event["model"],
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
                    self._upsert_agent(event)
                    self._upsert_turn(event)
        return inserted

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
                       t.measurement_quality, t.error_category, t.error_code,
                       t.harness, t.model, t.endpoint_id
                FROM turns t JOIN agents a ON a.id = t.agent_id
                {where} ORDER BY t.started_at DESC LIMIT ? OFFSET ?
                """,
                (*values, limit, max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

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
        for row in rows:
            outcome = row.get("outcome") or "active"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        terminal = sum(value for key, value in outcomes.items() if key != "active")
        return {
            "turn_count": len(rows),
            "active_turns": outcomes.get("active", 0),
            "outcomes": outcomes,
            "success_rate": outcomes.get("completed", 0) / terminal if terminal else None,
            "failure_rate": outcomes.get("failed", 0) / terminal if terminal else None,
            "cancellation_rate": outcomes.get("cancelled", 0) / terminal if terminal else None,
            "metrics": {
                name: cls._metric(rows, name)
                for name in ("ttfa_ms", "ttfvt_ms", "first_tool_ms", "duration_ms", "max_stall_ms")
            },
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
        return {
            "fleet": {"active_agents": sum(agent["current_turn_id"] is not None for agent in agents), **self._aggregate(rows)},
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
        terminal_model_events = [
            event
            for event in model_events
            if event["event_type"] in {"model.completed", "model.failed"}
        ]
        ttft_values = [
            float(event["attributes"]["elapsed_ms"])
            for event in model_events
            if event["event_type"] == "model.first_token"
            and "elapsed_ms" in event["attributes"]
        ]
        exact_decode_events = [
            event
            for event in terminal_model_events
            if event["event_type"] == "model.completed"
            and event["attributes"].get("correlation") == "exact"
            and isinstance(event["attributes"].get("decode_ms"), (int, float))
            and event["attributes"].get("decode_ms", 0) > 0
            and isinstance(event["attributes"].get("output_tokens"), int)
        ]
        decode_ms = sum(float(event["attributes"]["decode_ms"]) for event in exact_decode_events)
        output_tokens = sum(int(event["attributes"]["output_tokens"]) for event in exact_decode_events)
        correlations: dict[str, int] = {}
        for event in terminal_model_events:
            correlation = str(event["attributes"].get("correlation", "unavailable"))
            correlations[correlation] = correlations.get(correlation, 0) + 1

        model_metrics = {
            "call_count": sum(
                event["event_type"] == "model.request_started" for event in model_events
            ),
            "completed_count": sum(
                event["event_type"] == "model.completed" for event in model_events
            ),
            "failed_count": sum(event["event_type"] == "model.failed" for event in model_events),
            "ttft_ms": {
                "count": len(ttft_values),
                "p50": self._percentile(ttft_values, 0.50),
                "p95": self._percentile(ttft_values, 0.95),
                "minimum": min(ttft_values) if ttft_values else None,
                "maximum": max(ttft_values) if ttft_values else None,
            },
            "exact_output_tokens": output_tokens,
            "exact_decode_ms": decode_ms or None,
            "output_tokens_per_second": output_tokens / (decode_ms / 1000)
            if decode_ms > 0
            else None,
            "correlation_counts": correlations,
        }

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
