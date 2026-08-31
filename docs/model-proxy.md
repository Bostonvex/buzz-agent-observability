# Optional OpenAI- and Anthropic-compatible model timing proxy

The model proxy adds exact HTTP timing and exact response usage when a model
server exposes it. It is optional: ACP-only telemetry and every harness remain
supported with the proxy off.

The implementation follows the current OpenAI shapes for
[Chat Completions streaming chunks](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions)
and [Responses usage/status fields](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).
It also works with compatible local servers that preserve those fields.
Anthropic Messages support follows the documented
[streaming event sequence](https://platform.claude.com/docs/en/build-with-claude/streaming):
`message_start`, content-block events, cumulative usage in `message_delta`, and
in-stream `error` events.

## Security and privacy boundary

- The listener binds only to a loopback address and defaults to an ephemeral
  port.
- Every model request goes to one startup-configured upstream origin. Only
  `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, and
  `/v1/messages` are allowed by default; a request cannot choose a host.
- Request bodies are streamed without JSON parsing. Authorization and other
  upstream headers are forwarded but never logged or stored.
- `X-Buzz-Telemetry-*` correlation headers are removed before forwarding.
- Response inspection is capped at 8 MiB. It extracts only generated-chunk
  presence and non-negative usage counters, then discards its parser buffer.
- Collector delivery uses a bounded background queue. A missing collector,
  timeout, invalid private file, or rejected event never changes the model
  response.
- The DeepSeek, Qwen, and ZCode launchers start the proxy with a minimal environment
  allowlist. Model/API credentials stay only in the model client; collector
  token paths and proxy controls are removed from the model child environment.
- The authenticated context endpoint accepts only normalized identifiers and
  safe labels already produced by the ACP observer. It uses the private
  collector token and never accepts prompt or response fields.

No content-capture option exists.

## Start it

Start the collector first, then run:

```bash
buzz-model-proxy \
  --upstream http://127.0.0.1:8000 \
  --model example-model \
  --endpoint-id local-model-primary
```

The command prints its ephemeral loopback URL. Configure the model client's
OpenAI-compatible base URL to that address. The upstream API key remains in the
model client's normal `Authorization` header; do not pass it as a proxy
argument.

To correlate model calls with ACP turns, set this variable in the instrumented
harness after choosing the proxy port:

```text
BUZZ_MODEL_PROXY_CONTEXT_URL=http://127.0.0.1:<port>/__buzz/context
```

The shared observer posts start/end context records asynchronously. If exactly
one turn is active, correlation is `exact`. With simultaneous turns it is
`ambiguous` unless the model request supplies the matching
`X-Buzz-Telemetry-Context-Id`; the proxy strips that header before forwarding.
With no active context, correlation is `unavailable` or `ambiguous` according
to whether a static agent identity was configured.

### Supervised harness sidecar

The DeepSeek, Qwen, and ZCode harness integrations can supervise one ephemeral proxy
per harness process. Add these variables alongside the normal telemetry
configuration:

```text
BUZZ_MODEL_PROXY_ENABLED=1
BUZZ_MODEL_PROXY_BIN=/absolute/path/to/buzz-model-proxy
```

`BUZZ_MODEL_PROXY_BIN` must be absolute. The launcher starts the proxy on an
ephemeral loopback port, redirects only that process's model base URL, and
wires the observer to its authenticated context endpoint. The original model
upstream remains fixed for the proxy lifetime. If configuration is missing or
startup fails, the launcher reports a metadata-only diagnostic and uses the
original upstream. If an active proxy exits unexpectedly, the harness process
exits so Buzz can restart the complete process tree instead of silently losing
model measurements.

`BUZZ_MODEL_PROXY_STARTUP_TIMEOUT_MS` optionally changes the 3000 ms startup
deadline and is bounded to 100–30000 ms. It normally should not be set.

## Metrics and semantics

For streaming responses, the proxy records:

- upstream connection time;
- first response-body byte;
- first non-empty text, tool-call, function-argument, reasoning-summary, or
  refusal delta;
- final byte and total duration;
- decode time from first generated delta to final byte; and
- input, output, cached, and reasoning tokens when present.

For Anthropic streams, non-empty `text_delta`, `thinking_delta`, and
`input_json_delta` payloads count as generated output; a `tool_use` block start
also counts. Initial usage is read from `message_start`, final cumulative output
usage from `message_delta`, and an SSE `error` becomes a metadata-only failed
model call even when the HTTP status was 200. Event content is never retained.

For non-streaming responses, connection, first byte, final byte, status, and
usage are exact. TTFT and decode time are unavailable because a complete JSON
response cannot reveal when its first token was generated. Output throughput is
shown only when both output tokens and decode time are exact. Per-turn
throughput is available in the turn detail. **Fleet output tok/s** is a weighted
aggregate: total exact output tokens divided by total exact decode seconds
across the current dashboard filters. It is intentionally not an average of
per-call rates. The card shows both the number of exactly measured calls that
contributed and how many of those calls were attributed to an ACP turn.
Unattributed calls contribute to the fleet rate but never appear as agents or
in per-agent metrics.

The dashboard aligns cross-process model events by wall-clock timestamps;
process-local monotonic clocks are used only for duration calculations.

## Compatibility and rollback

The proxy preserves upstream status, response headers, response bytes, SSE
data, tool-call deltas, and error bodies. Client disconnects close the upstream
connection and emit a metadata-only cancellation failure. Incoming requests
must use `Content-Length`; chunked request uploads are rejected. The default
request cap is 256 MiB.

The supervised mode supports DeepSeek and Qwen with OpenAI-compatible
upstreams and ZCode with its Anthropic Messages upstream. Base-path prefixes
such as `/api/anthropic` are preserved exactly once when `/v1/messages` is
forwarded.

Rollback is immediate. In supervised mode set
`BUZZ_MODEL_PROXY_ENABLED=0` or remove both `BUZZ_MODEL_PROXY_*` variables and
restart Buzz. In manual mode restore the original model base URL and remove
`BUZZ_MODEL_PROXY_CONTEXT_URL`. No database migration or harness reinstall is
required.

## Measured overhead

Release acceptance is less than 5 ms added p50 latency on a synthetic loopback
non-streaming request. On 2026-08-31, 50 requests on the development workstation
measured 0.273 ms direct p50 and 0.480 ms proxy p50: **0.206 ms added p50**. The
proxy p95 was 0.715 ms. This measures local transport overhead, not model
latency, and should be repeated on release hardware.

The automated suite covers byte parity, SSE streaming, tool-call deltas,
Responses events, cancellation, 429/error preservation, a multi-megabyte
request, exact/ambiguous correlation, content exclusion, fixed paths, and
loopback binding. A real-provider smoke test is intentionally operator-run so
CI never requires or exposes an API key.

On 2026-08-31, an operator-run streaming canary through the installed sidecar
and configured DeepSeek-compatible endpoint returned HTTP 200 and recorded
exact correlation, 9 input tokens, 3 output tokens, 253.5 ms decode time, and
11.83 output tokens/second. No request or response content was retained.
