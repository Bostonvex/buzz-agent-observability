# Roadmap

1. **Collector foundation (current):** strict event schema, authenticated ingestion, SQLite WAL storage, live SSE, static health dashboard, retention, and security tests.
2. **Common ACP observer:** dependency-free Node.js state machine, identity hashing, bounded batching, monotonic timing, drop counters, and fail-open delivery.
3. **DeepSeek MVP:** per-agent turn, text, tool, cancellation, and process telemetry with existing protocol behavior preserved.
4. **Agent and turn views:** fleet summaries, p50/p95 metrics, filters, quality badges, and turn waterfalls.
5. **Qwen Code integration:** transparent piped ACP forwarding with byte/order/backpressure tests.
6. **ZCode integration:** native TypeScript lifecycle hooks with replay and background-task protection.
7. **Optional model timing proxy:** exact model TTFT and usage without content capture.
8. **Model/hardware providers and packaging:** vLLM, optional read-only hardware collectors, service examples, backup, upgrade, rollback, and uninstall.

Harness integrations remain separate reviewed changes. Phase 1 deliberately contains no model proxy, SSH collector, hardware command execution, or harness modification.
