# Qwen Code integration

Qwen telemetry is integrated in `buzz-qwen-code-harness` commit `948613a`.
The launcher replaces inherited stdio with native piped forwarding and inserts
a bounded NDJSON observation transform only when telemetry is validly enabled.
When telemetry is disabled, the launcher uses direct native pipes.

The transform forwards every input chunk unchanged, propagates downstream
backpressure, handles arbitrary chunk and CRLF boundaries, and limits retained
line data to 1 MiB by default. Malformed and oversized lines still pass through
unchanged; only their direction, anomaly kind, and byte count may be emitted.

Configuration uses the same shared variables documented in the
[DeepSeek integration](deepseek.md). Agent attribution comes from the original
ACP `session/new` metadata and is stored as a salted HMAC identifier with an
approved display label.

The Qwen acceptance suite covers recorded safe client/server fixtures, byte and
line preservation, slow-consumer backpressure, malformed and oversized lines,
observer exceptions, enabled/disabled parity, collector outage, exact child
exit codes and signals, tool updates, and two concurrent agent processes. It
also checks that prompt text, synthetic private values, and raw session IDs do
not enter telemetry.
