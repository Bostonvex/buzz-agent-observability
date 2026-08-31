# Buzz Agent Observability

Local-first, metadata-only observability for coding agents launched by Buzz. The project observes real Agent Client Protocol (ACP) turns while treating telemetry as disposable: an unavailable collector must never break an agent turn.

This repository is at the Phase 1 foundation. It provides:

- A collector bound exclusively to `127.0.0.1` on port `7900` by default.
- Strict version 1 event validation with field and attribute allowlists.
- Bearer-authenticated, bounded batch ingestion.
- SQLite persistence in WAL mode with seven-day raw-event retention.
- Sanitized live summaries over Server-Sent Events.
- Read-only agent and turn tables in a static dashboard.
- A metadata-only synthetic demo and standard-library test suite.

It does **not** yet modify or instrument any Buzz harness. DeepSeek integration begins after the shared fail-open ACP observer is implemented in Phase 2.

## Quick start

Python 3.11 or newer is required; the collector has no runtime dependencies.

```bash
./scripts/doctor.sh
./scripts/test.sh
python3 -m collector serve
```

Open <http://127.0.0.1:7900/>. In a second terminal, load two safe synthetic turns:

```bash
python3 -m collector demo
```

The first server start creates an ingest token at `~/.config/buzz-agent-observability/ingest-token` with mode `0600` and a database at `~/.local/share/buzz-agent-observability/telemetry.sqlite3`. Override these with `--token-file` and `--database`, or the corresponding `BUZZ_OBSERVABILITY_TOKEN_FILE` and `BUZZ_OBSERVABILITY_DATABASE` environment variables.

## API foundation

| Endpoint | Purpose | Authentication |
|---|---|---|
| `GET /healthz` | Collector and database health | Loopback only |
| `POST /api/v1/events` | One event or a batch of at most 100 | Bearer token |
| `GET /api/v1/live` | Sanitized live summaries over SSE | Loopback only |
| `GET /api/v1/agents` | Latest agent state | Loopback only |
| `GET /api/v1/turns` | Recent turn timing | Loopback only |

Request bodies are capped at 256 KiB. The server refuses a non-loopback bind in this phase. It has no endpoint that starts models, runs shell commands, controls SSH, purges data, or accepts an upstream URL.

## Privacy boundary

The schema rejects unknown fields and attributes. It has no fields for prompts, completions, reasoning text, tool arguments or results, filesystem paths, environment dumps, headers, cookies, authentication tokens, or private keys. Sensitive-looking values are rejected before SQLite insertion. See [Privacy](docs/privacy.md) and the [event contract](docs/event-schema.md).

## Project direction

The next phase introduces a small dependency-free Node.js ACP observer with bounded asynchronous delivery and no-throw behavior. DeepSeek is the first harness integration after that. See the [roadmap](docs/roadmap.md) and [proposed Phase 2 interface](docs/phase-2-interface.md).

## Upstream research

The project draws on presentation and model-health ideas from [2Wild Coding Agent Latency Monitor](https://github.com/tonyd2wild/2Wild-Coding-Agent-Latency-Monitor). The Phase 1 server and dashboard are clean-room implementations; no upstream source was copied. The pinned review, license decision, and security inventory are in [docs/upstream-review.md](docs/upstream-review.md).

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Please report vulnerabilities through GitHub private vulnerability reporting as described in [SECURITY.md](SECURITY.md).
