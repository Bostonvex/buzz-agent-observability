"""Command-line interface for the collector, diagnostics, and synthetic demo."""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from collector.auth import load_or_create_token
from collector.schema import validate_event
from collector.server import AppState, create_server
from collector.storage import TelemetryStore

DEFAULT_DATABASE = "~/.local/share/buzz-agent-observability/telemetry.sqlite3"
DEFAULT_TOKEN_FILE = "~/.config/buzz-agent-observability/ingest-token"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event(
    event_type: str,
    *,
    agent_id: str,
    display_name: str,
    turn_id: str | None,
    elapsed: float,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "observed_at": _now(),
        "monotonic_offset_ms": elapsed,
        "producer": {"name": "synthetic-demo", "version": "0.1.0", "instance_id": "demo-process"},
        "agent": {"id": agent_id, "display_name": display_name},
        "harness": "deepseek",
        "model": "example-model",
        "endpoint_id": "local-example",
        "session_id": f"session-{agent_id}",
        "turn_id": turn_id,
        "span_id": None,
        "parent_span_id": None,
        "attributes": attributes or {},
    }


def command_serve(args: argparse.Namespace) -> int:
    if args.raw_event_days < 1 or args.raw_event_days > 3650:
        raise SystemExit("--raw-event-days must be between 1 and 3650")
    token = load_or_create_token(args.token_file)
    store = TelemetryStore(args.database)
    package_dashboard = Path(__file__).resolve().parent / "dashboard"
    source_dashboard = Path(__file__).resolve().parent.parent / "dashboard"
    dashboard_dir = package_dashboard if package_dashboard.is_dir() else source_dashboard
    state = AppState(store, token, dashboard_dir, retention_days=args.raw_event_days)
    state.maintain_retention()
    server = create_server(host=args.host, port=args.port, state=state)
    actual_host, actual_port = server.server_address
    print(f"Buzz Agent Observability listening on http://{actual_host}:{actual_port}/")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping collector")
    finally:
        state.stopping.set()
        server.server_close()
        store.close()
    return 0


def _run_doctor(database: Path, token_file: Path) -> dict[str, Any]:
    token = load_or_create_token(token_file)
    store = TelemetryStore(database)
    try:
        sample = _event(
            "turn.started",
            agent_id="doctor-agent",
            display_name="Doctor agent",
            turn_id="doctor-turn",
            elapsed=0,
        )
        validate_event(sample)
        mode = stat.S_IMODE(token_file.stat().st_mode)
        health = store.health()
        return {
            "loopback_host": "ok",
            "event_schema": "ok",
            "database": health["status"],
            "journal_mode": health["journal_mode"],
            "token_file_mode": oct(mode),
            "token_length": len(token),
        }
    finally:
        store.close()


def command_doctor(args: argparse.Namespace) -> int:
    if args.database and args.token_file:
        result = _run_doctor(Path(args.database).expanduser(), Path(args.token_file).expanduser())
    elif args.database or args.token_file:
        raise SystemExit("doctor requires both --database and --token-file when either is supplied")
    else:
        with tempfile.TemporaryDirectory(prefix="buzz-observability-doctor-") as directory:
            root = Path(directory)
            result = _run_doctor(root / "telemetry.sqlite3", root / "ingest-token")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_demo(args: argparse.Namespace) -> int:
    token = load_or_create_token(args.token_file)
    events: list[dict[str, Any]] = []
    for index, display_name in enumerate(("Synthetic implementor", "Synthetic reviewer"), start=1):
        agent_id = f"demo-agent-{index}"
        turn_id = f"demo-turn-{uuid.uuid4()}"
        events.extend(
            [
                _event("turn.started", agent_id=agent_id, display_name=display_name, turn_id=turn_id, elapsed=0),
                _event(
                    "turn.first_activity",
                    agent_id=agent_id,
                    display_name=display_name,
                    turn_id=turn_id,
                    elapsed=120 + index * 30,
                    attributes={"elapsed_ms": 120 + index * 30, "measurement_quality": "exact", "update_kind": "agent_message_chunk"},
                ),
                _event(
                    "turn.first_visible_text",
                    agent_id=agent_id,
                    display_name=display_name,
                    turn_id=turn_id,
                    elapsed=180 + index * 40,
                    attributes={"elapsed_ms": 180 + index * 40, "measurement_quality": "exact"},
                ),
                _event(
                    "turn.completed",
                    agent_id=agent_id,
                    display_name=display_name,
                    turn_id=turn_id,
                    elapsed=900 + index * 150,
                    attributes={
                        "duration_ms": 900 + index * 150,
                        "ttfa_ms": 120 + index * 30,
                        "ttfvt_ms": 180 + index * 40,
                        "max_stall_ms": 210,
                        "tool_count": index - 1,
                        "outcome": "completed",
                        "measurement_quality": "exact",
                    },
                ),
            ]
        )
    body = json.dumps(events, separators=(",", ":")).encode("utf-8")
    request = Request(
        args.url.rstrip("/") + "/api/v1/events",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            result = json.load(response)
    except HTTPError as error:
        raise SystemExit(f"collector rejected synthetic events with HTTP {error.code}") from None
    except URLError:
        raise SystemExit("collector is unavailable; start it before loading the demo") from None
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first telemetry for Buzz coding agents")
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="run the loopback collector and dashboard")
    serve.add_argument("--host", default=os.environ.get("BUZZ_OBSERVABILITY_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("BUZZ_OBSERVABILITY_PORT", "7900")))
    serve.add_argument("--database", default=os.environ.get("BUZZ_OBSERVABILITY_DATABASE", DEFAULT_DATABASE))
    serve.add_argument("--token-file", default=os.environ.get("BUZZ_OBSERVABILITY_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    serve.add_argument("--raw-event-days", type=int, default=7)
    serve.set_defaults(handler=command_serve)

    doctor = subcommands.add_parser("doctor", help="validate loopback, token, schema, and SQLite behavior")
    doctor.add_argument("--database")
    doctor.add_argument("--token-file")
    doctor.set_defaults(handler=command_doctor)

    demo = subcommands.add_parser("demo", help="ingest metadata-only synthetic agent turns")
    demo.add_argument("--url", default="http://127.0.0.1:7900")
    demo.add_argument("--token-file", default=os.environ.get("BUZZ_OBSERVABILITY_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    demo.set_defaults(handler=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        args = parser.parse_args(["serve", *(argv or [])])
    return int(args.handler(args))
