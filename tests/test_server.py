from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from collector.auth import load_or_create_identity_salt, load_or_create_token
from collector.server import MAX_BODY_BYTES, AppState, create_server
from collector.storage import TelemetryStore
from tests.helpers import event


class CollectorServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "telemetry.sqlite3"
        self.token = "local-test-token-" + "x" * 32
        self.dashboard = Path(__file__).resolve().parent.parent / "dashboard"
        self._start()

    def tearDown(self) -> None:
        self._stop()
        self.temporary.cleanup()

    def _start(self) -> None:
        self.store = TelemetryStore(self.database)
        self.state = AppState(self.store, self.token, self.dashboard)
        self.server = create_server(host="127.0.0.1", port=0, state=self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _stop(self) -> None:
        self.state.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()

    def _request(
        self,
        path: str,
        *,
        data: bytes | None = None,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.url + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def test_health_and_static_dashboard(self) -> None:
        status, health = self._request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["journal_mode"], "wal")
        with urlopen(self.url + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("Buzz Agent Observability", page)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_token_failure_is_rejected(self) -> None:
        body = json.dumps(event()).encode("utf-8")
        status, response = self._request("/api/v1/events", data=body, token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(response["error"]["code"], "invalid_token")

    def test_unknown_field_is_rejected(self) -> None:
        submitted = event()
        submitted["unexpected"] = True
        status, response = self._request(
            "/api/v1/events",
            data=json.dumps(submitted).encode("utf-8"),
            token=self.token,
        )
        self.assertEqual(status, 422)
        self.assertEqual(response["error"]["code"], "unknown_field")

    def test_content_field_is_rejected(self) -> None:
        submitted = event(attributes={"content": "synthetic"})
        status, response = self._request(
            "/api/v1/events",
            data=json.dumps(submitted).encode("utf-8"),
            token=self.token,
        )
        self.assertEqual(status, 422)
        self.assertEqual(response["error"]["code"], "unknown_attribute")

    def test_oversized_body_is_rejected_before_json_parsing(self) -> None:
        status, response = self._request(
            "/api/v1/events",
            data=b"x" * (MAX_BODY_BYTES + 1),
            token=self.token,
        )
        self.assertEqual(status, 413)
        self.assertEqual(response["error"]["code"], "body_too_large")

    def test_event_survives_collector_restart(self) -> None:
        body = json.dumps(event()).encode("utf-8")
        status, response = self._request("/api/v1/events", data=body, token=self.token)
        self.assertEqual(status, 202)
        self.assertEqual(response["inserted"], 1)

        self._stop()
        self._start()
        status, health = self._request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["events"], 1)
        status, agents = self._request("/api/v1/agents")
        self.assertEqual(status, 200)
        self.assertEqual(agents["agents"][0]["display_name"], "Agent Alpha")

    def test_non_loopback_bind_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server(host="0.0.0.0", port=0, state=self.state)

    def test_node_observer_delivers_schema_valid_events_over_http(self) -> None:
        token_path = Path(self.temporary.name) / "observer-token"
        salt_path = Path(self.temporary.name) / "observer-salt"
        token_path.write_text(self.token + "\n", encoding="ascii")
        os.chmod(token_path, 0o600)
        load_or_create_token(token_path)
        load_or_create_identity_salt(salt_path)
        environment = {
            **os.environ,
            "BUZZ_TELEMETRY_ENABLED": "1",
            "BUZZ_TELEMETRY_URL": self.url + "/api/v1/events",
            "BUZZ_TELEMETRY_TOKEN_FILE": str(token_path),
            "BUZZ_TELEMETRY_IDENTITY_SALT_FILE": str(salt_path),
            "BUZZ_TELEMETRY_ENDPOINT_ID": "local-example",
            "BUZZ_TELEMETRY_AGENT_ID": "http-fixture-agent",
            "BUZZ_ACP_DISPLAY_NAME": "HTTP fixture agent",
        }
        completed = subprocess.run(
            ["node", "packages/acp-observer/test/http-fixture.mjs"],
            cwd=Path(__file__).resolve().parent.parent,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        diagnostics = json.loads(completed.stdout)
        self.assertEqual(diagnostics["transport"]["failedBatches"], 0)
        self.assertEqual(diagnostics["transport"]["droppedEvents"], 0)
        self.assertGreaterEqual(self.store.health()["events"], 6)
        self.assertEqual(self.store.list_agents()[0]["display_name"], "HTTP fixture agent")


if __name__ == "__main__":
    unittest.main()
