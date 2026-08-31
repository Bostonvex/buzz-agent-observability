import assert from "node:assert/strict";
import test from "node:test";

import { createTelemetryTransport, normalizeCollectorUrl } from "../src/index.js";

test("collector transport accepts loopback only", () => {
  assert.equal(
    normalizeCollectorUrl("http://127.0.0.1:7900/api/v1/events"),
    "http://127.0.0.1:7900/api/v1/events",
  );
  assert.throws(() => normalizeCollectorUrl("http://192.0.2.10:7900/api/v1/events"), /loopback/u);
  assert.throws(() => normalizeCollectorUrl("https://example.invalid/api/v1/events"), /loopback/u);
  assert.throws(() => normalizeCollectorUrl("http://user:pass@127.0.0.1:7900/api/v1/events"), /credentials/u);
});

test("transport batches events and flushes within a deadline", async () => {
  const batches = [];
  const transport = createTelemetryTransport({
    sendBatch: async (events) => batches.push(events),
    capacity: 10,
    batchSize: 2,
    flushIntervalMs: 10_000,
  });
  transport.enqueue({ id: 1 });
  transport.enqueue({ id: 2 });
  transport.enqueue({ id: 3 });
  await transport.flush({ deadlineMs: 500 });
  assert.deepEqual(batches, [[{ id: 1 }, { id: 2 }], [{ id: 3 }]]);
  assert.equal(transport.diagnostics().queueSize, 0);
  assert.equal(transport.diagnostics().sentEvents, 3);
});

test("queue and retries remain bounded during collector outage", async () => {
  const transport = createTelemetryTransport({
    sendBatch: async () => {
      throw new Error("collector unavailable");
    },
    capacity: 3,
    batchSize: 3,
    flushIntervalMs: 10_000,
    maxAttempts: 1,
  });
  for (let index = 0; index < 10; index += 1) transport.enqueue({ id: index });
  assert.equal(transport.diagnostics().queueSize, 3);
  assert.equal(transport.diagnostics().droppedEvents, 7);
  await transport.flush({ deadlineMs: 500 });
  assert.equal(transport.diagnostics().queueSize, 0);
  assert.equal(transport.diagnostics().droppedEvents, 10);
  assert.equal(transport.diagnostics().failedBatches, 1);
});

test("slow fetch is aborted without rejecting flush", async () => {
  const transport = createTelemetryTransport({
    collectorUrl: "http://127.0.0.1:7900/api/v1/events",
    token: "x".repeat(40),
    fetchImpl: async (_url, options) =>
      new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      }),
    timeoutMs: 10,
    maxAttempts: 1,
  });
  transport.enqueue({ id: 1 });
  await assert.doesNotReject(transport.flush({ deadlineMs: 100 }));
  assert.equal(transport.diagnostics().failedBatches, 1);
  assert.equal(transport.diagnostics().droppedEvents, 1);
});

test("invalid numeric configuration falls back to bounded defaults", () => {
  const transport = createTelemetryTransport({
    sendBatch: async () => {},
    capacity: Number.NaN,
    batchSize: Number.POSITIVE_INFINITY,
    timeoutMs: -1,
  });
  assert.equal(transport.diagnostics().capacity, 1024);
});
