"""Strict JSON-command provider with fixed argv and metric allowlists."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

from collector.providers.base import InfrastructureSample, safe_identifier
from collector.providers.command import run_bounded

MAX_CONFIG_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_JSON_SAMPLES = 128
CONFIG_FIELDS = {
    "schema_version",
    "scope",
    "provider_id",
    "node_id",
    "endpoint_id",
    "argv",
    "allowed_metrics",
    "timeout_seconds",
}
SAMPLE_FIELDS = {"metric_name", "value", "unit", "measurement_quality"}


class JsonCommandProvider:
    name = "json-command"

    def __init__(
        self,
        *,
        scope: str,
        argv: list[str],
        allowed_metrics: set[str],
        provider_id: str | None = None,
        node_id: str | None = None,
        endpoint_id: str | None = None,
        timeout_seconds: float = 3.0,
        runner: Callable[..., bytes] = run_bounded,
    ) -> None:
        if scope not in {"server", "hardware"}:
            raise ValueError("JSON provider scope must be server or hardware")
        if not argv or not argv[0].startswith("/"):
            raise ValueError("JSON provider executable must be an absolute path")
        if len(argv) > 32 or any(not isinstance(item, str) or not item or len(item) > 2048 for item in argv):
            raise ValueError("JSON provider argv is invalid")
        if any(any(ord(character) < 32 for character in item) for item in argv):
            raise ValueError("JSON provider argv contains control characters")
        if not allowed_metrics or len(allowed_metrics) > 128:
            raise ValueError("JSON provider requires 1 to 128 allowed metrics")
        self.allowed_metrics = {safe_identifier(item, "allowed metric") for item in allowed_metrics}
        self.scope = scope
        self.argv = list(argv)
        self.provider_id = safe_identifier(provider_id, "provider_id") if scope == "hardware" else None
        self.node_id = safe_identifier(node_id, "node_id") if scope == "hardware" else None
        self.endpoint_id = safe_identifier(endpoint_id, "endpoint_id") if scope == "server" else None
        if scope == "hardware" and endpoint_id is not None:
            raise ValueError("hardware JSON providers cannot set endpoint_id")
        if scope == "server" and (provider_id is not None or node_id is not None):
            raise ValueError("server JSON providers cannot set hardware identity")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("JSON provider timeout must be between 0 and 30 seconds")
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    @classmethod
    def from_file(cls, path: str | Path, *, runner: Callable[..., bytes] = run_bounded) -> JsonCommandProvider:
        config_path = Path(path).expanduser()
        try:
            config_size = config_path.stat().st_size
        except OSError as error:
            raise ValueError("JSON provider config could not be read") from error
        if config_size > MAX_CONFIG_BYTES:
            raise ValueError("JSON provider config exceeded the limit")
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("JSON provider config could not be read") from error
        if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
            raise ValueError("JSON provider config fields do not match schema version 1")
        if config["schema_version"] != 1:
            raise ValueError("unsupported JSON provider config schema")
        if not isinstance(config["argv"], list) or not isinstance(config["allowed_metrics"], list):
            raise ValueError("JSON provider argv and allowed_metrics must be arrays")
        return cls(
            scope=config["scope"],
            argv=config["argv"],
            allowed_metrics=set(config["allowed_metrics"]),
            provider_id=config["provider_id"],
            node_id=config["node_id"],
            endpoint_id=config["endpoint_id"],
            timeout_seconds=config["timeout_seconds"],
            runner=runner,
        )

    def poll(self) -> list[InfrastructureSample]:
        body = self.runner(self.argv, timeout_seconds=self.timeout_seconds, maximum_bytes=MAX_OUTPUT_BYTES)
        try:
            submitted = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("JSON provider output is invalid") from error
        if not isinstance(submitted, dict) or set(submitted) != {"schema_version", "samples"}:
            raise ValueError("JSON provider output fields do not match schema")
        if submitted["schema_version"] != 1 or not isinstance(submitted["samples"], list):
            raise ValueError("JSON provider output schema is invalid")
        if len(submitted["samples"]) > MAX_JSON_SAMPLES:
            raise ValueError("JSON provider returned too many samples")

        samples: list[InfrastructureSample] = []
        for item in submitted["samples"]:
            if not isinstance(item, dict) or set(item) != SAMPLE_FIELDS:
                raise ValueError("JSON provider sample fields do not match schema")
            metric_name = item["metric_name"]
            if metric_name not in self.allowed_metrics:
                raise ValueError("JSON provider returned a metric outside the allowlist")
            value = item["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("JSON provider returned an invalid value")
            samples.append(
                InfrastructureSample(
                    self.scope,
                    metric_name,
                    value,
                    item["unit"],
                    provider_id=self.provider_id,
                    node_id=self.node_id,
                    endpoint_id=self.endpoint_id,
                    measurement_quality=item["measurement_quality"],
                )
            )
        return samples
