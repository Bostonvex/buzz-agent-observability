export class RecordingTransport {
  constructor({ throwOnEnqueue = false } = {}) {
    this.events = [];
    this.throwOnEnqueue = throwOnEnqueue;
    this.closed = false;
  }

  enqueue(event) {
    if (this.throwOnEnqueue) throw new Error("synthetic transport failure");
    this.events.push(event);
    return true;
  }

  async flush() {
    return this.diagnostics();
  }

  async close() {
    this.closed = true;
  }

  diagnostics() {
    return { queueSize: 0, droppedEvents: 0, sentEvents: this.events.length, closed: this.closed };
  }
}

export function deterministicUuid() {
  let count = 0;
  return () => {
    count += 1;
    return `00000000-0000-4000-8000-${String(count).padStart(12, "0")}`;
  };
}

export function createClock(start = 0) {
  let value = start;
  return {
    now: () => value,
    set: (next) => {
      value = next;
    },
    advance: (amount) => {
      value += amount;
    },
  };
}

export function baseConfig(overrides = {}) {
  const clock = overrides.testClock ?? createClock();
  return {
    enabled: true,
    identitySalt: "test-identity-salt-with-enough-entropy",
    harness: "deepseek",
    harnessVersion: "0.1.0",
    model: "example-model",
    endpointId: "local-example",
    producerName: "test-harness",
    producerVersion: "0.1.0",
    randomUUID: deterministicUuid(),
    clock: clock.now,
    wallClock: () => new Date("2026-08-31T12:00:00.000Z"),
    transport: overrides.transport ?? new RecordingTransport(),
    stallThresholdMs: 100,
    ...overrides,
    testClock: undefined,
  };
}
