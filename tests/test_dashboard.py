from __future__ import annotations

import unittest
from pathlib import Path


class DashboardSafetyTests(unittest.TestCase):
    def test_dynamic_metadata_uses_text_content(self) -> None:
        script = (Path(__file__).resolve().parent.parent / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)

    def test_phase_four_states_and_views_are_present(self) -> None:
        root = Path(__file__).resolve().parent.parent / "dashboard"
        page = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        for identifier in ("fleet-view", "agent-view", "turn-view", "status-banner", "shared-context"):
            self.assertIn(f'id="{identifier}"', page)
        for label in ("exact", "derived", "estimated", "unavailable"):
            self.assertIn(label, page + script)
        self.assertIn("EventSource", script)
        self.assertIn("Collector disconnected", script)
        self.assertIn("URLSearchParams", script)


if __name__ == "__main__":
    unittest.main()
