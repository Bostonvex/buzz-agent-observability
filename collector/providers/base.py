"""Shared provider types, event construction, and failure isolation."""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol

from collector import __version__
from collector.schema import validate_event

LOGGER = logging.getLogger("buzz_observability.providers")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@=-]{0,255}$")
MAX_SAMPLES_PER_POLL = 128


def safe_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a safe non-empty identifier")
    return value


@dataclass(frozen=True, slots=True)
class InfrastructureSample:
    """One allowlisted shared metric, never attributed to an individual agent."""

    scope: str
    metric_name: str
    value: float
    unit: str
    endpoint_id: str | None = None
    provider_id: str | None = None
    node_id: str | None = None
    measurement_quality: str = "exact"

    def __post_init__(self) -> None:
        if self.scope not in {"server", "hardware"}:
            raise ValueError("scope must be server or hardware")
        safe_identifier(self.metric_name, "metric_name")
        safe_identifier(self.unit, "unit")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("value must be numeric")
        if not math.isfinite(float(self.value)) or self.value < 0 or self.value > 10**15:
            raise ValueError("value is outside the safe numeric range")
        if self.measurement_quality not in {"exact", "derived", "estimated", "unavailable"}:
            raise ValueError("invalid measurement_quality")
        if self.scope == "server":
            safe_identifier(self.endpoint_id, "endpoint_id")
            if self.provider_id is not None or self.node_id is not None:
                raise ValueError("server samples cannot include hardware identity")
        else:
            safe_identifier(self.provider_id, "provider_id")
            safe_identifier(self.node_id, "node_id")
            if self.endpoint_id is not None:
                raise ValueError("hardware samples cannot include endpoint_id")


class Provider(Protocol):
    name: str

    def poll(self) -> list[InfrastructureSample]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sample_event(
    sample: InfrastructureSample,
    *,
    instance_id: str,
    monotonic_offset_ms: float,
) -> dict[str, object]:
    attributes: dict[str, object] = {
        "metric_name": sample.metric_name,
        "value": sample.value,
        "unit": sample.unit,
        "measurement_quality": sample.measurement_quality,
    }
    if sample.scope == "hardware":
        attributes["provider_id"] = sample.provider_id
        attributes["node_id"] = sample.node_id
    event = {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_type": f"{sample.scope}.sample",
        "observed_at": _utc_now(),
        "monotonic_offset_ms": monotonic_offset_ms,
        "producer": {
            "name": "buzz-infrastructure-provider",
            "version": __version__,
            "instance_id": instance_id,
        },
        "agent": {"id": "shared-infrastructure", "display_name": "Shared infrastructure"},
        "harness": None,
        "model": None,
        "endpoint_id": sample.endpoint_id,
        "session_id": None,
        "turn_id": None,
        "span_id": None,
        "parent_span_id": None,
        "attributes": attributes,
    }
    return validate_event(event)


class ProviderSupervisor:
    """Run each optional provider independently and drop failures safely."""

    def __init__(
        self,
        providers: Iterable[Provider],
        emit: Callable[[list[dict[str, object]]], None],
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        if interval_seconds < 1 or interval_seconds > 3600:
            raise ValueError("provider interval must be between 1 and 3600 seconds")
        self.providers = tuple(providers)
        self.emit = emit
        self.interval_seconds = interval_seconds
        self.instance_id = str(uuid.uuid4())
        self.started_at = time.monotonic()
        self.stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._status = {
            provider.name: {"status": "pending", "samples": 0, "failures": 0}
            for provider in self.providers
        }

    def start(self) -> None:
        for provider in self.providers:
            thread = threading.Thread(
                target=self._run,
                args=(provider,),
                name=f"buzz-provider-{provider.name}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self.stopping.set()
        deadline = time.monotonic() + max(0, timeout)
        for thread in self._threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def diagnostics(self) -> dict[str, dict[str, int | str]]:
        with self._lock:
            return {name: dict(status) for name, status in self._status.items()}

    def _record(self, name: str, *, status: str, samples: int = 0, failed: bool = False) -> None:
        with self._lock:
            current = self._status[name]
            current["status"] = status
            current["samples"] = int(current["samples"]) + samples
            if failed:
                current["failures"] = int(current["failures"]) + 1

    def _run(self, provider: Provider) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                samples = provider.poll()
                if len(samples) > MAX_SAMPLES_PER_POLL:
                    raise ValueError("provider returned too many samples")
                events = [
                    sample_event(
                        sample,
                        instance_id=self.instance_id,
                        monotonic_offset_ms=(time.monotonic() - self.started_at) * 1000,
                    )
                    for sample in samples
                ]
                if events:
                    self.emit(events)
                self._record(provider.name, status="ok", samples=len(events))
            except Exception as error:
                self._record(provider.name, status="degraded", failed=True)
                LOGGER.warning(
                    "optional provider %s poll failed (%s); collection continues",
                    provider.name,
                    type(error).__name__,
                )
            elapsed = time.monotonic() - started
            self.stopping.wait(max(0.05, self.interval_seconds - elapsed))
