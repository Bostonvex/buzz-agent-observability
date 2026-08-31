# Optional OpenAI-compatible model timing proxy

The model proxy adds exact HTTP timing and exact response usage when a model
server exposes it. It is optional: ACP-only telemetry and every harness remain
supported with the proxy off.

The implementation follows the current OpenAI shapes for
[Chat Completions streaming chunks](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions)
and [Responses usage/status fields](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).
It also works with compatible local servers that preserve those fields.

## Security and privacy boundary

- The listener binds only to a loopback address and defaults to an ephemeral
  port.
- Every model request goes to one startup-configured upstream origin. Only
  `/v1/chat/completions`, `/v1/completions`, and `/v1/responses` are allowed by
  default; a request cannot choose a host.
- Request bodies are streamed without JSON parsing. Authorization and other
  upstream headers are forwarded but never logged or stored.
- `X-Buzz-Telemetry-*` correlation headers are removed before forwarding.
- Response inspection is capped at 8 MiB. It extracts only generated-chunk
  presence and non-negative usage counters, then discards its parser buffer.
- Collector delivery uses a bounded background queue. A missing collector,
  timeout, invalid private file, or rejected event never changes the model
  response.
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

## Metrics and semantics

For streaming responses, the proxy records:

- upstream connection time;
- first response-body byte;
- first non-empty text, tool-call, function-argument, reasoning-summary, or
  refusal delta;
- final byte and total duration;
- decode time from first generated delta to final byte; and
- input, output, cached, and reasoning tokens when present.

For non-streaming responses, connection, first byte, final byte, status, and
usage are exact. TTFT and decode time are unavailable because a complete JSON
response cannot reveal when its first token was generated. Output throughput is
shown only when both output tokens and decode time are exact.

The dashboard aligns cross-process model events by wall-clock timestamps;
process-local monotonic clocks are used only for duration calculations.

## Compatibility and rollback

The proxy preserves upstream status, response headers, response bytes, SSE
data, tool-call deltas, and error bodies. Client disconnects close the upstream
connection and emit a metadata-only cancellation failure. Incoming requests
must use `Content-Length`; chunked request uploads are rejected. The default
request cap is 256 MiB.

Rollback is immediate: restore the original model base URL and remove
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
