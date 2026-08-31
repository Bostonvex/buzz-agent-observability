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
        for identifier in (
            "fleet-view", "agent-view", "turn-view", "status-banner", "shared-context",
            "shared-charts", "fleet-output-tps", "fleet-output-tps-quality",
            "fleet-output-tps-coverage",
        ):
            self.assertIn(f'id="{identifier}"', page)
        for label in ("exact", "derived", "estimated", "unavailable"):
            self.assertIn(label, page + script)
        self.assertIn("EventSource", script)
        self.assertIn("Collector disconnected", script)
        self.assertIn("URLSearchParams", script)
        self.assertIn("Model p50 TTFT", script)
        self.assertIn("Model output tok/s", script)
        self.assertIn("Fleet output tok/s", page)
        self.assertIn("output_tokens_per_second", script)
        self.assertIn("relative_ms", script)
        self.assertIn("/api/v1/samples", script)
        self.assertIn("never assigned to an individual agent", page)

    def test_recent_turns_are_sortable_and_expandable(self) -> None:
        root = Path(__file__).resolve().parent.parent / "dashboard"
        page = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        for field in (
            "agent_display_name", "outcome", "started_at", "ended_at", "ttfa_ms",
            "ttfvt_ms", "first_tool_ms", "duration_ms", "output_tokens_per_second",
            "tool_count", "measurement_quality",
        ):
            self.assertIn(f'data-turn-sort="{field}"', page)
        self.assertIn('turnSort: { key: "started_at", direction: "desc" }', script)
        self.assertIn("turns.slice(0, 10)", script)
        self.assertIn('id="toggle-turns"', page)
        self.assertIn('aria-expanded="false"', page)


if __name__ == "__main__":
    unittest.main()
