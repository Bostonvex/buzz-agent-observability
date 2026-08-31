from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from collector.providers.base import InfrastructureSample, ProviderSupervisor, sample_event
from collector.providers.json_command import JsonCommandProvider
from collector.providers.nvidia_smi import NvidiaSmiProvider
from collector.providers.vllm import parse_prometheus_metrics, validate_metrics_url
from collector.storage import TelemetryStore


class VllmProviderTests(unittest.TestCase):
    def test_only_allowlisted_metrics_are_aggregated_without_labels(self) -> None:
        body = b"""# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="private/model-a"} 2
vllm:num_requests_running{model_name="private/model-b"} 3
vllm:gpu_cache_usage_perc{model_name="private/model-a"} 0.5
vllm:gpu_cache_usage_perc{model_name="private/model-b"} 0.7
vllm:time_to_first_token_seconds_sum{model_name="private/model-a"} 1.5
vllm:time_to_first_token_seconds_count{model_name="private/model-a"} 3
untrusted:metric{secret="do-not-store"} 999
"""
        samples = parse_prometheus_metrics(body, "local-vllm")
        by_name = {sample.metric_name: sample for sample in samples}
        self.assertEqual(by_name["requests_running"].value, 5)
        self.assertAlmostEqual(by_name["gpu_kv_cache_usage_ratio"].value, 0.6)
        self.assertEqual(by_name["request_ttft_mean_seconds"].value, 0.5)
        self.assertEqual(by_name["request_ttft_mean_seconds"].measurement_quality, "derived")
        self.assertNotIn("private", json.dumps([sample.metric_name for sample in samples]))

    def test_metrics_url_is_fixed_and_has_no_credentials(self) -> None:
        self.assertEqual(validate_metrics_url("http://model.example:8000/metrics"), "http://model.example:8000/metrics")
        for invalid in (
            "http://user:password@model.example/metrics",
            "http://model.example/v1/models",
            "http://model.example/metrics?token=value",
            "file:///metrics",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_metrics_url(invalid)


class NvidiaProviderTests(unittest.TestCase):
    def test_local_query_is_fixed_and_parsed(self) -> None:
        captured: list[list[str]] = []

        def runner(argv, **kwargs):  # type: ignore[no-untyped-def]
            captured.append(list(argv))
            return b"0, 75, 1024, 8192, 58, 111.25\n1, N/A, 0, 8192, 49, 80\n"

        provider = NvidiaSmiProvider("gpu-node", nvidia_smi="/usr/bin/nvidia-smi", runner=runner)
        samples = provider.poll()
        self.assertEqual(captured[0][0], "/usr/bin/nvidia-smi")
        self.assertIn("--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", captured[0])
        self.assertEqual(len(samples), 9)
        self.assertEqual(samples[0].metric_name, "gpu.0.utilization_percent")
        self.assertTrue(all(sample.node_id == "gpu-node" for sample in samples))

    def test_remote_query_requires_normal_host_verification(self) -> None:
        provider = NvidiaSmiProvider(
            "remote-node",
            remote_host="telemetry@example.test",
            ssh="/usr/bin/ssh",
            runner=lambda *args, **kwargs: b"",
        )
        joined = " ".join(provider.argv)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("BatchMode=yes", joined)
        self.assertNotIn("StrictHostKeyChecking=no", joined)
        with self.assertRaises(ValueError):
            NvidiaSmiProvider("remote-node", remote_host="host;touch bad", ssh="/usr/bin/ssh")


class JsonCommandProviderTests(unittest.TestCase):
    def test_fixed_argv_and_metric_allowlist(self) -> None:
        config = {
            "schema_version": 1,
            "scope": "hardware",
            "provider_id": "custom-readonly",
            "node_id": "node-a",
            "endpoint_id": None,
            "argv": ["/opt/telemetry/read-only-sample", "--json"],
            "allowed_metrics": ["accelerator.utilization_percent"],
            "timeout_seconds": 2,
        }
        output = {
            "schema_version": 1,
            "samples": [
                {
                    "metric_name": "accelerator.utilization_percent",
                    "value": 44.5,
                    "unit": "percent",
                    "measurement_quality": "exact",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            provider = JsonCommandProvider.from_file(
                path,
                runner=lambda *args, **kwargs: json.dumps(output).encode("utf-8"),
            )
            samples = provider.poll()
        self.assertEqual(samples[0].value, 44.5)
        self.assertEqual(provider.argv, ["/opt/telemetry/read-only-sample", "--json"])

        output["samples"][0]["metric_name"] = "not.allowlisted"
        provider.runner = lambda *args, **kwargs: json.dumps(output).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            provider.poll()


class SupervisorTests(unittest.TestCase):
    def test_provider_failure_is_isolated_and_shared_samples_do_not_create_agents(self) -> None:
        class Good:
            name = "good"

            def poll(self):  # type: ignore[no-untyped-def]
                return [InfrastructureSample("server", "requests_running", 1, "requests", endpoint_id="test")]

        class Bad:
            name = "bad"

            def poll(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("synthetic failure")

        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "telemetry.sqlite3")

            def emit(events):  # type: ignore[no-untyped-def]
                store.insert_events(events)

            supervisor = ProviderSupervisor([Good(), Bad()], emit, interval_seconds=1)
            supervisor.start()
            deadline = time.monotonic() + 1
            while supervisor.diagnostics()["good"]["samples"] == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            supervisor.stop()
            diagnostics = supervisor.diagnostics()
            self.assertEqual(diagnostics["good"]["status"], "ok")
            self.assertEqual(diagnostics["bad"]["status"], "degraded")
            self.assertEqual(store.health()["events"], 1)
            self.assertEqual(store.health()["agents"], 0)
            self.assertEqual(store.list_samples()[0]["attributes"]["metric_name"], "requests_running")
            store.close()

    def test_sample_event_validates_as_shared_context(self) -> None:
        event = sample_event(
            InfrastructureSample(
                "hardware",
                "gpu.0.temperature_celsius",
                60,
                "celsius",
                provider_id="nvidia-smi",
                node_id="node-a",
            ),
            instance_id="test-instance",
            monotonic_offset_ms=10,
        )
        self.assertEqual(event["agent"]["id"], "shared-infrastructure")
        self.assertIsNone(event["turn_id"])


if __name__ == "__main__":
    unittest.main()
