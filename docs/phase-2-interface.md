# Proposed Phase 2 ACP observer interface

The common observer will be a small dependency-free Node.js package with a synchronous observation surface and asynchronous best-effort transport:

```js
const observer = createAcpObserver(config)
observer.observeClientMessage(message, monotonicNow)
observer.observeServerMessage(message, monotonicNow)
observer.observeProcessExit(details, monotonicNow)
await observer.flush({ deadlineMs: 50 })
```

The observation methods must never throw into protocol forwarding. They update bounded session, turn, and tool state, enqueue schema-versioned metadata-only events, and return immediately. Delivery uses a bounded batch queue with short request deadlines; exhaustion increments a drop counter and discards telemetry rather than delaying ACP.

The configuration surface should include collector URL, token-file path, harness/model/endpoint labels, producer version, stall threshold, maximum active sessions and tools, queue capacity, batch size, and send timeout. It must not accept content-capture flags.

Identity resolution will accept only an explicit non-secret per-agent identifier or already validated non-secret Buzz session metadata. A local collector salt produces the stable HMAC ID. Owner authorization tags and private key material are prohibited inputs and should be rejected in tests.
