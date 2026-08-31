# DeepSeek Harness integration

DeepSeek telemetry is integrated in `buzz-deepseek-harness` commit
`97c0e6c`. The bridge observes parsed ACP messages at its existing
client/server forwarding boundary and sends only schema-versioned metadata to
the local collector.

## Enable it

Start the collector once so it creates the private ingest token and identity
salt, then add these variables to the DeepSeek bridge process environment:

```text
BUZZ_TELEMETRY_ENABLED=1
BUZZ_TELEMETRY_URL=http://127.0.0.1:7900/api/v1/events
BUZZ_TELEMETRY_TOKEN_FILE=~/.config/buzz-agent-observability/ingest-token
BUZZ_TELEMETRY_IDENTITY_SALT_FILE=~/.config/buzz-agent-observability/identity-salt
BUZZ_TELEMETRY_ENDPOINT_ID=local-model-primary
```

Agent attribution uses `BUZZ_TELEMETRY_AGENT_ID`,
`BUZZ_ACP_DISPLAY_NAME`, or `BUZZ_GIT_ORIGIN_AGENT_NAME`. Values found in the
original ACP `session/new` MCP metadata are read before the bridge removes the
MCP configuration from the message forwarded to DeepSeek. The collector stores
only a salted HMAC identifier and the approved display label.

## Failure behavior

Telemetry is disabled by default. Missing configuration, invalid private-file
permissions, a stopped collector, timeouts, and rejected batches all fail open:
ACP requests and responses continue unchanged. Exit delivery is bounded to 50
milliseconds. Telemetry never writes to protocol stdout.

The integration test starts two bridge processes, proves distinct agent,
process, and session attribution, checks that raw session IDs and synthetic
private values do not reach the collector, and verifies ACP parity while the
collector is unavailable.
