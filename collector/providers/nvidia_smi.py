"""Optional local or strict-host-verified remote NVIDIA telemetry."""

from __future__ import annotations

import math
import re
import shutil
from collections.abc import Callable, Sequence

from collector.providers.base import InfrastructureSample, safe_identifier
from collector.providers.command import run_bounded

QUERY = "index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
FORMAT = "csv,noheader,nounits"
REMOTE_HOST = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
Runner = Callable[..., bytes]


def _absolute_executable(value: str, name: str) -> str:
    resolved = value if value.startswith("/") else shutil.which(value)
    if not resolved or not resolved.startswith("/"):
        raise ValueError(f"{name} executable was not found")
    return resolved


def _number(value: str) -> float | None:
    if value.strip().lower() in {"n/a", "na", "not supported", "[not supported]"}:
        return None
    parsed = float(value.strip())
    if not math.isfinite(parsed) or parsed < 0 or parsed > 10**15:
        raise ValueError("nvidia-smi returned an invalid number")
    return parsed


class NvidiaSmiProvider:
    name = "nvidia-smi"

    def __init__(
        self,
        node_id: str,
        *,
        remote_host: str | None = None,
        nvidia_smi: str = "nvidia-smi",
        ssh: str = "ssh",
        timeout_seconds: float = 3.0,
        runner: Runner = run_bounded,
    ) -> None:
        self.node_id = safe_identifier(node_id, "node_id")
        self.remote_host = remote_host
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if remote_host is None:
            self.argv = [
                _absolute_executable(nvidia_smi, "nvidia-smi"),
                f"--query-gpu={QUERY}",
                f"--format={FORMAT}",
            ]
        else:
            if not REMOTE_HOST.fullmatch(remote_host):
                raise ValueError("remote NVIDIA host is not a safe SSH destination")
            self.name = "nvidia-smi-remote"
            self.argv = [
                _absolute_executable(ssh, "ssh"),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"ConnectTimeout={max(1, min(30, int(timeout_seconds)))}",
                "--",
                remote_host,
                "nvidia-smi",
                f"--query-gpu={QUERY}",
                f"--format={FORMAT}",
            ]

    def poll(self) -> list[InfrastructureSample]:
        body = self.runner(self.argv, timeout_seconds=self.timeout_seconds, maximum_bytes=256 * 1024)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("nvidia-smi output is not UTF-8") from error
        samples: list[InfrastructureSample] = []
        definitions: Sequence[tuple[str, str]] = (
            ("utilization_percent", "percent"),
            ("memory_used_mib", "MiB"),
            ("memory_total_mib", "MiB"),
            ("temperature_celsius", "celsius"),
            ("power_watts", "watts"),
        )
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 6 or not fields[0].isdigit() or int(fields[0]) > 1024:
                raise ValueError("nvidia-smi output does not match the allowlisted query")
            index = int(fields[0])
            for raw, (metric, unit) in zip(fields[1:], definitions, strict=True):
                value = _number(raw)
                if value is not None:
                    samples.append(
                        InfrastructureSample(
                            "hardware",
                            f"gpu.{index}.{metric}",
                            value,
                            unit,
                            provider_id="nvidia-smi",
                            node_id=self.node_id,
                        )
                    )
        return samples
