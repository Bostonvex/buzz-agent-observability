# ACP observer package

`@buzz-agent-observability/acp-observer` passively observes already-parsed ACP JSON-RPC messages and emits version 1 metadata-only events. It is dependency-free, bounded, asynchronous, no-throw, and fail-open.

```js
import { createAcpObserverFromEnv } from "@buzz-agent-observability/acp-observer";

const observer = createAcpObserverFromEnv({
  harness: "deepseek",
  harnessVersion: "0.1.0",
  model: "example-model",
  producerName: "buzz-deepseek-harness",
  producerVersion: "0.1.0",
});

observer.observeClientMessage(parsedClientMessage, performance.now());
observer.observeServerMessage(parsedServerMessage, performance.now());
observer.observeProcessExit({ code, signal }, performance.now());
await observer.flush({ deadlineMs: 50 });
```

The observer never records message content, tool arguments or results, paths, headers, credentials, or arbitrary metadata. Telemetry is disabled unless `BUZZ_TELEMETRY_ENABLED=1` and private token/salt files are configured. Collector delivery accepts only loopback HTTP URLs.
