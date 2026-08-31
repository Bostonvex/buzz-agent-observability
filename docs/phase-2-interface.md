# Phase 2 ACP observer interface

The common observer is a dependency-free Node.js package with a synchronous observation surface and asynchronous best-effort transport:

```js
const observer = createAcpObserver(config)
observer.observeClientMessage(message, monotonicNow)
observer.observeServerMessage(message, monotonicNow)
observer.observeProcessExit(details, monotonicNow)
await observer.flush({ deadlineMs: 50 })
```

The observation methods never throw into protocol forwarding. They update bounded session, turn, and tool state, enqueue schema-versioned metadata-only events, and return immediately. Delivery uses a bounded batch queue with short request deadlines and one bounded retry; exhaustion increments a drop counter and discards telemetry rather than delaying ACP.

The configuration surface includes collector URL, private token and identity-salt files, harness/model/endpoint labels, producer version, stall threshold, maximum active sessions, turns, tools, and pending requests, queue capacity, batch size, and send timeout. It has no content-capture flags. `createAcpObserverFromEnv` is disabled unless explicitly enabled and refuses non-loopback collector URLs.

Identity resolution accepts only an explicit non-secret per-agent identifier or already validated non-secret Buzz session metadata. A local collector salt produces stable HMAC agent and session IDs. Owner authorization tags and private key material are never read; tests use throwing accessors to enforce that boundary.
