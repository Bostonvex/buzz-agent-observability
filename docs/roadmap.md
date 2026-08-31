# Roadmap

1. **Collector foundation (complete):** strict event schema, authenticated ingestion, SQLite WAL storage, live SSE, static health dashboard, retention, and security tests.
2. **Common ACP observer (complete):** dependency-free Node.js state machine, identity hashing, bounded batching, monotonic timing, drop counters, and fail-open delivery.
3. **DeepSeek MVP (complete):** per-agent turn, text, tool, cancellation, and process telemetry with existing protocol behavior preserved.
4. **Agent and turn views (complete):** fleet summaries, p50/p95 metrics, filters, quality badges, and turn waterfalls.
5. **Qwen Code integration (complete):** transparent piped ACP forwarding with byte/order/backpressure tests.
6. **ZCode integration (complete):** native TypeScript lifecycle hooks with replay and background-task protection.
7. **Optional model timing proxy (complete):** exact model TTFT and usage without content capture.
8. **Model/hardware providers and packaging (complete):** vLLM, optional read-only hardware collectors, service examples, backup, upgrade, rollback, and recoverable uninstall.

Harness integrations remain separate reviewed changes. All optional providers and the model proxy are disabled by default; ACP-only installation needs no model-node modification.
