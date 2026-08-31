# Release notes

## 0.1.0 development release

- Three ACP harness integrations share one metadata-only event contract.
- The dashboard includes filtered fleet, agent, turn-waterfall, and exact model
  timing views.
- The optional OpenAI-compatible sidecar preserves streaming and non-streaming
  responses while recording connection, first-byte, first-generated-chunk,
  completion, and usage metadata.
- Synthetic loopback proxy overhead measured 0.206 ms added p50 across 50
  non-streaming requests, below the 5 ms release threshold. See the
  [model proxy guide](model-proxy.md) for method and limitations.
