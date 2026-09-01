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

from collector.auth import load_or_create_identity_salt, load_or_create_token
from collector.providers import JsonCommandProvider, NvidiaSmiProvider, ProviderSupervisor, VllmMetricsProvider
from collector.providers.base import Provider, sample_event
from collector.schema import validate_event
from collector.server import AppState, create_server
from collector.storage import TelemetryStore

DEFAULT_DATABASE = "~/.local/share/buzz-agent-observability/telemetry.sqlite3"
DEFAULT_TOKEN_FILE = "~/.config/buzz-agent-observability/ingest-token"
DEFAULT_IDENTITY_SALT_FILE = "~/.config/buzz-agent-observability/identity-salt"


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
    if args.host != "127.0.0.1":
        raise SystemExit("the collector permits only the literal loopback host 127.0.0.1")
    providers = _configured_providers(args)
    token = load_or_create_token(args.token_file)
    load_or_create_identity_salt(args.identity_salt_file)
    store = TelemetryStore(args.database)
    package_dashboard = Path(__file__).resolve().parent / "dashboard"
    source_dashboard = Path(__file__).resolve().parent.parent / "dashboard"
    dashboard_dir = package_dashboard if package_dashboard.is_dir() else source_dashboard
    state = AppState(store, token, dashboard_dir, retention_days=args.raw_event_days)
    supervisor: ProviderSupervisor | None = None
    server = None
    try:
        state.maintain_retention()
        server = create_server(host=args.host, port=args.port, state=state)
        if providers:
            def emit(events: list[dict[str, object]]) -> None:
                store.insert_events(events)
                for event in events:
                    state.broker.publish(event)

            supervisor = ProviderSupervisor(providers, emit, interval_seconds=args.provider_interval)
            state.provider_diagnostics = supervisor.diagnostics
            supervisor.start()
        actual_host, actual_port = server.server_address
        print(f"Buzz Agent Observability listening on http://{actual_host}:{actual_port}/")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping collector")
    finally:
        state.stopping.set()
        if supervisor is not None:
            supervisor.stop()
        if server is not None:
            server.server_close()
        store.close()
    return 0


def _run_doctor(
    database: Path,
    token_file: Path,
    identity_salt_file: Path,
    providers: list[Provider] | None = None,
) -> tuple[dict[str, Any], bool]:
    token = load_or_create_token(token_file)
    identity_salt = load_or_create_identity_salt(identity_salt_file)
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
        result = {
            "loopback_host": "ok",
            "event_schema": "ok",
            "database": health["status"],
            "journal_mode": health["journal_mode"],
            "token_file_mode": oct(mode),
            "token_length": len(token),
            "identity_salt_file_mode": oct(stat.S_IMODE(identity_salt_file.stat().st_mode)),
            "identity_salt_length": len(identity_salt),
        }
        healthy = True
        provider_results: dict[str, dict[str, Any]] = {}
        for provider in providers or []:
            try:
                samples = provider.poll()
                for item in samples:
                    sample_event(item, instance_id="doctor", monotonic_offset_ms=0)
                provider_results[provider.name] = {"status": "ok", "samples": len(samples)}
            except Exception as error:
                healthy = False
                provider_results[provider.name] = {
                    "status": "degraded",
                    "error_type": type(error).__name__,
                }
        result["providers"] = provider_results
        return result, healthy
    finally:
        store.close()


def command_doctor(args: argparse.Namespace) -> int:
    providers = _configured_providers(args)
    if args.database and args.token_file and args.identity_salt_file:
        result, healthy = _run_doctor(
            Path(args.database).expanduser(),
            Path(args.token_file).expanduser(),
            Path(args.identity_salt_file).expanduser(),
            providers,
        )
    elif args.database or args.token_file or args.identity_salt_file:
        raise SystemExit(
            "doctor requires --database, --token-file, and --identity-salt-file when any is supplied"
        )
    else:
        with tempfile.TemporaryDirectory(prefix="buzz-observability-doctor-") as directory:
            root = Path(directory)
            result, healthy = _run_doctor(
                root / "telemetry.sqlite3", root / "ingest-token", root / "identity-salt", providers
            )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if healthy else 1


def _configured_providers(args: argparse.Namespace) -> list[Provider]:
    providers: list[Provider] = []
    try:
        if getattr(args, "vllm_metrics_url", None):
            providers.append(VllmMetricsProvider(args.vllm_metrics_url, args.vllm_endpoint_id))
        if getattr(args, "nvidia_smi", False):
            providers.append(NvidiaSmiProvider(args.nvidia_node_id))
        if getattr(args, "nvidia_ssh_host", None):
            providers.append(
                NvidiaSmiProvider(
                    args.nvidia_ssh_node_id,
                    remote_host=args.nvidia_ssh_host,
                )
            )
        for path in getattr(args, "json_provider_config", []) or []:
            providers.append(JsonCommandProvider.from_file(path))
    except (OSError, ValueError) as error:
        raise SystemExit(f"provider configuration is invalid ({type(error).__name__})") from None
    names = [provider.name for provider in providers]
    if len(names) != len(set(names)):
        raise SystemExit("configure at most one provider of each type")
    return providers


def command_backup(args: argparse.Namespace) -> int:
    database = Path(args.database).expanduser()
    if not database.is_file():
        raise SystemExit("backup database does not exist or is not a regular file")
    store = TelemetryStore(database)
    try:
        try:
            destination = store.backup_to(args.output)
        except FileExistsError:
            raise SystemExit("backup destination already exists") from None
        health = store.health()
    finally:
        store.close()
    print(json.dumps({"backup": str(destination), "events": health["events"], "turns": health["turns"]}, indent=2))
    return 0


def command_purge(args: argparse.Namespace) -> int:
    if not args.confirm_delete_raw_events:
        raise SystemExit("purge requires --confirm-delete-raw-events")
    try:
        before = datetime.fromisoformat(args.before.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit("--before must be an ISO 8601 timestamp with timezone") from None
    if before.tzinfo is None:
        raise SystemExit("--before must include a timezone")
    database = Path(args.database).expanduser()
    if not database.is_file():
        raise SystemExit("purge database does not exist or is not a regular file")
    store = TelemetryStore(database)
    try:
        deleted = store.purge_raw_before(before)
    finally:
        store.close()
    print(json.dumps({"deleted_raw_events": deleted, "turn_summaries_preserved": True}, indent=2))
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
                        "tool_observation_mode": "acp_updates",
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
    serve.add_argument(
        "--identity-salt-file",
        default=os.environ.get("BUZZ_OBSERVABILITY_IDENTITY_SALT_FILE", DEFAULT_IDENTITY_SALT_FILE),
    )
    serve.add_argument("--raw-event-days", type=int, default=7)
    _add_provider_arguments(serve, include_interval=True)
    serve.set_defaults(handler=command_serve)

    doctor = subcommands.add_parser("doctor", help="validate loopback, token, schema, and SQLite behavior")
    doctor.add_argument("--database")
    doctor.add_argument("--token-file")
    doctor.add_argument("--identity-salt-file")
    _add_provider_arguments(doctor, include_interval=False)
    doctor.set_defaults(handler=command_doctor)

    demo = subcommands.add_parser("demo", help="ingest metadata-only synthetic agent turns")
    demo.add_argument("--url", default="http://127.0.0.1:7900")
    demo.add_argument("--token-file", default=os.environ.get("BUZZ_OBSERVABILITY_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    demo.set_defaults(handler=command_demo)

    backup = subcommands.add_parser("backup", help="create a consistent, mode-0600 SQLite backup")
    backup.add_argument("--database", default=os.environ.get("BUZZ_OBSERVABILITY_DATABASE", DEFAULT_DATABASE))
    backup.add_argument("--output", required=True)
    backup.set_defaults(handler=command_backup)

    purge = subcommands.add_parser("purge", help="delete raw events before a timestamp; turn summaries remain")
    purge.add_argument("--database", default=os.environ.get("BUZZ_OBSERVABILITY_DATABASE", DEFAULT_DATABASE))
    purge.add_argument("--before", required=True)
    purge.add_argument("--confirm-delete-raw-events", action="store_true")
    purge.set_defaults(handler=command_purge)
    return parser


def _add_provider_arguments(parser: argparse.ArgumentParser, *, include_interval: bool) -> None:
    group = parser.add_argument_group("optional shared infrastructure providers")
    group.add_argument("--vllm-metrics-url")
    group.add_argument("--vllm-endpoint-id", default="vllm-primary")
    group.add_argument("--nvidia-smi", action="store_true", help="poll local nvidia-smi with a fixed query")
    group.add_argument("--nvidia-node-id", default="local-nvidia")
    group.add_argument("--nvidia-ssh-host", help="strict-host-verified SSH destination")
    group.add_argument("--nvidia-ssh-node-id", default="remote-nvidia")
    group.add_argument("--json-provider-config", action="append", default=[])
    if include_interval:
        group.add_argument("--provider-interval", type=float, default=10.0)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        args = parser.parse_args(["serve", *(argv or [])])
    return int(args.handler(args))
