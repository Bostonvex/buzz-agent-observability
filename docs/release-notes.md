# Release notes

## 0.1.0

- Three ACP harness integrations share one metadata-only event contract.
- The dashboard includes filtered fleet, agent, turn-waterfall, and exact model
  timing views.
- The optional OpenAI- and Anthropic-compatible sidecar preserves streaming and
  non-streaming responses while recording connection, first-byte,
  first-generated-chunk, completion, and usage metadata. Anthropic Messages
  includes cumulative streaming usage and HTTP-200 SSE error recognition.
- The dashboard includes weighted fleet output tokens/second across all exactly
  measured calls in the current filter scope, with measured-call and
  ACP-attribution coverage shown beside the metric. Unattributed model traffic
  does not create pseudo-agent rows.
- The inference-performance view adds TTFT and context-size distributions,
  median per-call throughput, weighted decode rate by concurrency band, and
  wall-clock generation throughput derived from reset-safe server counters.
- Synthetic loopback proxy overhead measured 0.206 ms added p50 across 50
  non-streaming requests, below the 5 ms release threshold. See the
  [model proxy guide](model-proxy.md) for method and limitations.
- Optional vLLM and NVIDIA providers add timestamp-correlated shared context
  without agent attribution. Generic commands are shell-free, output-bounded,
  and restricted by explicit metric allowlists.
- The dashboard graphs shared model-server and hardware series separately from
  agent measurements.
- Versioned local installation, live SQLite backup, diagnostics, upgrade,
  rollback, recoverable uninstall, launchd/systemd examples, archive safety
  checks, CodeQL, and reproducible checksums complete the operational release.

Known limitations:

- Real-provider smoke tests require operator-supplied endpoints or hardware and
  are not part of the hermetic acceptance suite.
- vLLM Prometheus metrics are server aggregates. Labels are discarded and
  histogram averages are derived; exact per-request timing comes from the
  optional model proxy.
- The model proxy requires `Content-Length` on requests and bounds response
  metadata inspection to 8 MiB. Non-streaming responses cannot expose a
  first-generated-chunk timestamp distinct from completion.
