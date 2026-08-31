from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from collector.schema import validate_event
from collector.storage import TelemetryStore
from tests.helpers import event


class TelemetryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "telemetry.sqlite3"
        self.store = TelemetryStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_wal_and_idempotent_insert(self) -> None:
        submitted = validate_event(event())
        self.assertEqual(self.store.journal_mode, "wal")
        self.assertEqual(self.store.insert_events([submitted]), 1)
        self.assertEqual(self.store.insert_events([submitted]), 0)
        self.assertEqual(self.store.health()["events"], 1)

    def test_agent_and_turn_summary(self) -> None:
        started = validate_event(event())
        completed = validate_event(
            event(
                "turn.completed",
                attributes={
                    "duration_ms": 820,
                    "ttfa_ms": 110,
                    "ttfvt_ms": 170,
                    "max_stall_ms": 90,
                    "tool_count": 2,
                    "measurement_quality": "exact",
                    "outcome": "completed",
                },
            )
        )
        self.assertEqual(self.store.insert_events([started, completed]), 2)
        agent = self.store.list_agents()[0]
        turn = self.store.list_turns()[0]
        self.assertEqual(agent["current_state"], "completed")
        self.assertIsNone(agent["current_turn_id"])
        self.assertEqual(turn["duration_ms"], 820)
        self.assertEqual(turn["tool_count"], 2)

    def test_raw_event_retention_does_not_remove_turn_summary(self) -> None:
        old = validate_event(event(observed_at="2020-01-01T00:00:00Z"))
        self.store.insert_events([old])
        deleted = self.store.purge_expired_raw(
            retention_days=7,
            now=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        self.assertEqual(deleted, 1)
        self.assertEqual(self.store.health()["events"], 0)
        self.assertEqual(self.store.health()["turns"], 1)

    def test_late_older_event_does_not_regress_agent_state(self) -> None:
        completed = validate_event(
            event(
                "turn.completed",
                observed_at="2026-08-31T12:01:00Z",
                attributes={"duration_ms": 100, "outcome": "completed"},
            )
        )
        older_started = validate_event(event(observed_at="2026-08-31T12:00:00Z"))
        self.store.insert_events([completed, older_started])
        agent = self.store.list_agents()[0]
        self.assertEqual(agent["current_state"], "completed")
        self.assertIsNone(agent["current_turn_id"])


if __name__ == "__main__":
    unittest.main()
