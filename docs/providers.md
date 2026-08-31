# Shared infrastructure providers

Infrastructure telemetry is optional. The ACP observer, collector, dashboard, and exports work without access to a model-serving node. Samples are stored as shared, timestamp-correlated context and are never presented as measurements of a particular agent.

Each provider runs in its own daemon thread. A timeout, malformed response, unavailable command, rejected SSH connection, oversized output, storage error, or validation failure degrades only that provider. Its status and cumulative sample/failure counts appear in `/healthz`.

## vLLM Prometheus

```bash
buzz-observability serve \
  --vllm-metrics-url http://model-node.example:8000/metrics \
  --vllm-endpoint-id vllm-primary \
  --provider-interval 10
```

The URL must be a fixed HTTP(S) `/metrics` endpoint with no credentials, query, or fragment. Redirects are rejected. Responses are capped at 2 MiB. Only documented scheduler, cache, token-counter, success, preemption, TTFT, queue, and end-to-end latency families are parsed. Prometheus labels—including model and deployment labels—are discarded rather than stored. Multi-series gauges are averaged, counters are summed, and histogram means are marked `derived`.

## NVIDIA

Local collection uses one fixed read-only query and does not require root:

```bash
buzz-observability serve --nvidia-smi --nvidia-node-id workstation-gpu
```

Remote collection invokes the local `ssh` executable with batch mode, a bounded connection timeout, and `StrictHostKeyChecking=yes`:

```bash
buzz-observability serve \
  --nvidia-ssh-host telemetry@accelerator.example \
  --nvidia-ssh-node-id accelerator-node-a
```

The remote destination accepts only a bounded hostname or `user@hostname`; shell metacharacters are rejected. The remote command is the fixed `nvidia-smi` query. The provider never changes SSH configuration, bypasses host verification, installs software remotely, or starts/stops a model server.

Collected per-GPU fields are utilization, used/total memory, temperature, and power draw. UUIDs, hostnames discovered from the machine, driver metadata, process lists, and command errors are not persisted.

## Generic JSON command

Use this interface for an explicitly trusted, read-only local executable. The collector invokes an absolute executable plus fixed arguments with `shell=False`, no stdin, discarded stderr, a timeout, and a 1 MiB stdout cap. The configuration fixes the scope, source identity, and exact metric allowlist.

Start from [the example configuration](../config/json-provider.example.json):

```bash
buzz-observability doctor --json-provider-config ./provider.json
buzz-observability serve --json-provider-config ./provider.json
```

All configuration fields are required. For `hardware`, `provider_id` and `node_id` are strings and `endpoint_id` is `null`. For `server`, `endpoint_id` is a string and both hardware identity fields are `null`.

The command must return exactly:

```json
{
  "schema_version": 1,
  "samples": [
    {
      "metric_name": "accelerator.utilization_percent",
      "value": 42.5,
      "unit": "percent",
      "measurement_quality": "exact"
    }
  ]
}
```

Unknown fields, unlisted metrics, non-finite/negative values, unsafe identifiers, more than 128 samples, and malformed JSON reject the entire poll. Do not configure a command that emits secrets, content, paths, unbounded data, or performs mutations.

## Diagnostics

Pass the same provider options to `doctor` to execute one isolated poll without persisting samples:

```bash
buzz-observability doctor \
  --vllm-metrics-url http://model-node.example:8000/metrics \
  --vllm-endpoint-id vllm-primary
```

The output reports only status, safe error class, and sample count. It does not echo command arguments, output, URLs, or remote addresses.
