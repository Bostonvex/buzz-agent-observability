"""Read-only, allowlisted polling of a vLLM Prometheus endpoint."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from collector.providers.base import InfrastructureSample, safe_identifier

MAX_METRICS_BYTES = 2 * 1024 * 1024
MAX_METRIC_LINE_BYTES = 8192
METRIC_LINE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^\r\n]*\})?\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?:\s+\d+)?$"
)

# Metrics are server-level context. Labels are deliberately discarded rather
# than persisted, because they can contain model names or deployment details.
SUM_METRICS = {
    "vllm:num_requests_running": ("requests_running", "requests"),
    "vllm:num_requests_waiting": ("requests_waiting", "requests"),
    "vllm:num_requests_swapped": ("requests_swapped", "requests"),
    "vllm:prompt_tokens_total": ("prompt_tokens_total", "tokens"),
    "vllm:generation_tokens_total": ("generation_tokens_total", "tokens"),
    "vllm:request_success_total": ("successful_requests_total", "requests"),
    "vllm:num_preemptions_total": ("preemptions_total", "requests"),
}
MEAN_METRICS = {
    "vllm:gpu_cache_usage_perc": ("gpu_kv_cache_usage_ratio", "ratio"),
    "vllm:cpu_cache_usage_perc": ("cpu_kv_cache_usage_ratio", "ratio"),
}
HISTOGRAM_MEANS = {
    "vllm:time_to_first_token_seconds": ("request_ttft_mean_seconds", "seconds"),
    "vllm:e2e_request_latency_seconds": ("request_e2e_latency_mean_seconds", "seconds"),
    "vllm:request_queue_time_seconds": ("request_queue_mean_seconds", "seconds"),
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
        return None


def validate_metrics_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("vLLM metrics URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("vLLM metrics URL cannot contain credentials")
    if parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/metrics":
        raise ValueError("vLLM metrics URL must be a fixed /metrics endpoint without query or fragment")
    if any(ord(character) < 32 for character in value) or len(value) > 2048:
        raise ValueError("vLLM metrics URL is unsafe")
    return value


def parse_prometheus_metrics(body: bytes, endpoint_id: str) -> list[InfrastructureSample]:
    if len(body) > MAX_METRICS_BYTES:
        raise ValueError("vLLM metrics response exceeded the limit")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("vLLM metrics response is not UTF-8") from error

    values: dict[str, list[float]] = defaultdict(list)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if len(line.encode("utf-8")) > MAX_METRIC_LINE_BYTES:
            raise ValueError("vLLM metric line exceeded the limit")
        match = METRIC_LINE.fullmatch(line)
        if match is None:
            continue
        name = match.group("name")
        if (
            name not in SUM_METRICS
            and name not in MEAN_METRICS
            and not any(name in {f"{base}_sum", f"{base}_count"} for base in HISTOGRAM_MEANS)
        ):
            continue
        value = float(match.group("value"))
        if math.isfinite(value) and 0 <= value <= 10**15:
            values[name].append(value)

    samples: list[InfrastructureSample] = []
    for source, (target, unit) in SUM_METRICS.items():
        if values[source]:
            samples.append(
                InfrastructureSample("server", target, sum(values[source]), unit, endpoint_id=endpoint_id)
            )
    for source, (target, unit) in MEAN_METRICS.items():
        if values[source]:
            samples.append(
                InfrastructureSample(
                    "server", target, sum(values[source]) / len(values[source]), unit, endpoint_id=endpoint_id
                )
            )
    for source, (target, unit) in HISTOGRAM_MEANS.items():
        total = sum(values[f"{source}_sum"])
        count = sum(values[f"{source}_count"])
        if count > 0:
            samples.append(
                InfrastructureSample(
                    "server", target, total / count, unit, endpoint_id=endpoint_id, measurement_quality="derived"
                )
            )
    return samples


class VllmMetricsProvider:
    name = "vllm"

    def __init__(self, metrics_url: str, endpoint_id: str, *, timeout_seconds: float = 3.0) -> None:
        self.metrics_url = validate_metrics_url(metrics_url)
        self.endpoint_id = safe_identifier(endpoint_id, "endpoint_id")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("vLLM timeout must be between 0 and 30 seconds")
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirect())

    def poll(self) -> list[InfrastructureSample]:
        request = Request(
            self.metrics_url,
            method="GET",
            headers={"Accept": "text/plain", "User-Agent": "buzz-agent-observability/0.1"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise ValueError("vLLM metrics endpoint returned a non-success status")
                body = response.read(MAX_METRICS_BYTES + 1)
        except HTTPError as error:
            error.close()
            raise ValueError("vLLM metrics endpoint rejected the request") from None
        return parse_prometheus_metrics(body, self.endpoint_id)
