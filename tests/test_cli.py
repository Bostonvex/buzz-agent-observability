from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from collector.cli import build_parser, main
from collector.schema import validate_event
from collector.storage import TelemetryStore
from tests.helpers import event


class OperationsCliTests(unittest.TestCase):
    def test_backup_and_confirmed_raw_purge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "telemetry.sqlite3"
            store = TelemetryStore(database)
            store.insert_events([validate_event(event(observed_at="2020-01-01T00:00:00Z"))])
            store.close()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["backup", "--database", str(database), "--output", str(root / "backup.sqlite3")]),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["events"], 1)
            with self.assertRaisesRegex(SystemExit, "confirm"):
                main(["purge", "--database", str(database), "--before", "2021-01-01T00:00:00Z"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "purge",
                            "--database",
                            str(database),
                            "--before",
                            "2021-01-01T00:00:00Z",
                            "--confirm-delete-raw-events",
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["deleted_raw_events"], 1)

    def test_provider_options_are_disabled_by_default(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["serve"])
        self.assertIsNone(args.vllm_metrics_url)
        self.assertFalse(args.nvidia_smi)
        self.assertIsNone(args.nvidia_ssh_host)
        self.assertEqual(args.json_provider_config, [])

    def test_project_scripts_never_manage_service_state(self) -> None:
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        text = "\n".join(
            (scripts / name).read_text(encoding="utf-8")
            for name in ("install.sh", "upgrade.sh", "rollback.sh", "uninstall.sh")
        )
        self.assertNotIn("launchctl", text)
        self.assertNotIn("systemctl", text)


if __name__ == "__main__":
    unittest.main()
