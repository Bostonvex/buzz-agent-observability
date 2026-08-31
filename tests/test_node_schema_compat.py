from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from collector.schema import validate_event


class NodeSchemaCompatibilityTests(unittest.TestCase):
    def test_observer_fixture_is_accepted_by_collector_validator(self) -> None:
        root = Path(__file__).resolve().parent.parent
        completed = subprocess.run(
            ["node", "packages/acp-observer/test/schema-fixture.mjs"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        events = json.loads(completed.stdout)
        self.assertGreater(len(events), 10)
        for event in events:
            with self.subTest(event_type=event["event_type"]):
                validate_event(event)
