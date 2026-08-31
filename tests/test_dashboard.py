from __future__ import annotations

import unittest
from pathlib import Path


class DashboardSafetyTests(unittest.TestCase):
    def test_dynamic_metadata_uses_text_content(self) -> None:
        script = (Path(__file__).resolve().parent.parent / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
