# Buzz Agent Observability

Local-first, metadata-only observability for coding agents launched by Buzz. It measures real Agent Client Protocol (ACP) turns, model timing, tools, stalls, outcomes, and optional shared infrastructure health while treating telemetry as disposable: an unavailable collector must never break an agent turn.

Release 0.1.0 includes:

- A loopback-only collector, SQLite history, live SSE, JSON APIs, and CSV export.
- Fleet, agent, turn-waterfall, exact model timing, and shared infrastructure views.
- A dependency-free, fail-open ACP observer integrated with the DeepSeek, Qwen Code, and ZCode harnesses.
- An optional fixed-upstream OpenAI- and Anthropic-compatible timing proxy.
- Optional vLLM, local NVIDIA, strict-host-verified remote NVIDIA, and generic JSON-command providers.
- Versioned installation, diagnostics, backup, upgrade, rollback, recoverable uninstall, and service examples.

No content-capture mode exists. Prompts, responses, reasoning, tool payloads, headers, credentials, filesystem paths, and environment dumps are outside the schema.

## Quick start

Python 3.11 or newer and Node.js 22 or newer are required for the full source test suite. The installed collector has no third-party runtime dependencies.

```bash
./scripts/doctor.sh
./scripts/test.sh
python3 -m collector serve
```

Open <http://127.0.0.1:7900/>. In another terminal, load safe synthetic turns with:

```bash
python3 -m collector demo
```

The first server start creates an ingest token and separate HMAC identity salt under `~/.config/buzz-agent-observability/`, both mode `0600`, and a database under `~/.local/share/buzz-agent-observability/`. Paths and port are configurable.

For an artifact-based workstation install:

```bash
uv build
python3 scripts/check-release.py --write-checksums
./scripts/install.sh --artifact dist/buzz_agent_observability-0.1.0-py3-none-any.whl
```

The installer does not start or enable a service. See [operations](docs/operations.md) for clean install, backup, upgrade, rollback, service, and uninstall procedures.

## Harness telemetry

The shared observer is disabled by default. An instrumented harness uses:

```text
BUZZ_TELEMETRY_ENABLED=1
BUZZ_TELEMETRY_URL=http://127.0.0.1:7900/api/v1/events
BUZZ_TELEMETRY_TOKEN_FILE=/path/to/ingest-token
BUZZ_TELEMETRY_IDENTITY_SALT_FILE=/path/to/identity-salt
BUZZ_TELEMETRY_ENDPOINT_ID=local-model-primary
```

Integration-specific instructions are available for [DeepSeek](integrations/deepseek.md), [Qwen Code](integrations/qwen-code.md), and [ZCode](integrations/zcode.md). The [observer package guide](packages/acp-observer/README.md) documents its API and fail-open guarantees.

## Optional model and infrastructure telemetry

`buzz-model-proxy` is a loopback-only, fixed-upstream sidecar for exact connection, first-byte, streaming TTFT, decode, duration, and usage metadata. It supports OpenAI-compatible endpoints and Anthropic Messages, preserves response bytes and errors, and can correlate with the active ACP turn. The dashboard reports weighted fleet throughput, per-call throughput, TTFT and input-token distributions, and decode rate by concurrency band. See the [model proxy guide](docs/model-proxy.md).

The collector can also poll shared infrastructure:

```bash
python3 -m collector serve \
  --vllm-metrics-url http://model-node.example:8000/metrics \
  --vllm-endpoint-id vllm-primary \
  --nvidia-smi \
  --nvidia-node-id workstation-gpu
```

All providers are optional and disabled unless configured. Provider failures are isolated from ingestion and agent execution. Server and hardware samples are timestamp-correlated fleet context; they are never assigned to an individual agent. When vLLM metrics are enabled, the dashboard also derives aggregate wall-clock generation tokens/second while safely handling counter resets. See [providers](docs/providers.md).

## Local API

| Endpoint | Purpose | Authentication |
|---|---|---|
| `GET /healthz` | Collector, database, and provider health | Loopback only |
| `POST /api/v1/events` | One event or a bounded batch | Bearer token |
| `GET /api/v1/live` | Sanitized live summaries over SSE | Loopback only |
| `GET /api/v1/agents` | Latest agent state | Loopback only |
| `GET /api/v1/turns` | Recent turn timing | Loopback only |
| `GET /api/v1/samples` | Shared model-server and hardware samples | Loopback only |
| `GET /api/v1/summary` | Filtered fleet aggregates | Loopback only |
| `GET /api/v1/export.csv` | Formula-safe turn export | Loopback only |

The event request body is capped at 256 KiB and 100 events. Query ranges, row counts, provider responses, command output, proxy bodies, and in-memory queues are bounded.

## Security and privacy

The collector accepts only the literal loopback bind `127.0.0.1`. The event schema rejects unknown fields and attributes as well as sensitive-looking values. Provider metric names are allowlisted; vLLM labels are discarded; subprocess providers never use a shell; remote NVIDIA polling requires normal SSH host verification.

Read [privacy](docs/privacy.md), the [threat model](docs/threat-model.md), [security policy](SECURITY.md), and the [event contract](docs/event-schema.md) before extending the schema or adding a provider.

## Project status

All eight planned phases are implemented in release 0.1.0. The [roadmap](docs/roadmap.md) records milestone status, and [release notes](docs/release-notes.md) record measured proxy overhead and known limitations. The presentation concepts were informed by [2Wild Coding Agent Latency Monitor](https://github.com/tonyd2wild/2Wild-Coding-Agent-Latency-Monitor); provenance and the clean-room decision are documented in [upstream review](docs/upstream-review.md).

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
