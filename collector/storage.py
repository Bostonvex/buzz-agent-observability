"""SQLite persistence for normalized metadata-only telemetry."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    duration_ms REAL,
    max_stall_ms REAL,
    tool_count INTEGER,
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
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (_utc_now(),),
            )
            self._connection.commit()

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
                ttfvt_ms, duration_ms, max_stall_ms, tool_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                started_at = MIN(turns.started_at, excluded.started_at),
                ended_at = COALESCE(excluded.ended_at, turns.ended_at),
                outcome = COALESCE(excluded.outcome, turns.outcome),
                ttfa_ms = COALESCE(excluded.ttfa_ms, turns.ttfa_ms),
                ttfvt_ms = COALESCE(excluded.ttfvt_ms, turns.ttfvt_ms),
                duration_ms = COALESCE(excluded.duration_ms, turns.duration_ms),
                max_stall_ms = CASE
                    WHEN excluded.max_stall_ms IS NULL THEN turns.max_stall_ms
                    WHEN turns.max_stall_ms IS NULL THEN excluded.max_stall_ms
                    ELSE MAX(turns.max_stall_ms, excluded.max_stall_ms)
                END,
                tool_count = COALESCE(excluded.tool_count, turns.tool_count)
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
                attributes.get("duration_ms"),
                attributes.get("max_stall_ms", attributes.get("gap_ms") if event_type == "turn.stall" else None),
                attributes.get("tool_count"),
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

    def list_agents(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, display_name, first_seen_at, last_seen_at, harness, model,
                       endpoint_id, current_state, current_turn_id
                FROM agents ORDER BY last_seen_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_turns(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT t.id, t.agent_id, a.display_name AS agent_display_name, t.session_id,
                       t.started_at, t.ended_at, t.outcome, t.ttfa_ms, t.ttfvt_ms,
                       t.duration_ms, t.max_stall_ms, t.tool_count
                FROM turns t JOIN agents a ON a.id = t.agent_id
                ORDER BY t.started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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
