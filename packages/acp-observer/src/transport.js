const DEFAULT_CAPACITY = 1024;
const DEFAULT_BATCH_SIZE = 50;
const DEFAULT_FLUSH_INTERVAL_MS = 250;
const DEFAULT_TIMEOUT_MS = 200;

function positiveInteger(value, fallback, maximum = Number.MAX_SAFE_INTEGER) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 1) return fallback;
  return Math.min(maximum, Math.floor(number));
}

function isLoopbackHostname(hostname) {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
}

export function normalizeCollectorUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "http:" || !isLoopbackHostname(url.hostname)) {
    throw new TypeError("collector URL must use HTTP on a loopback hostname");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new TypeError("collector URL must not contain credentials, query parameters, or fragments");
  }
  return url.toString();
}

async function settleWithin(promise, milliseconds) {
  let timer;
  try {
    await Promise.race([
      promise,
      new Promise((resolve) => {
        timer = setTimeout(resolve, milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export class TelemetryTransport {
  constructor({
    collectorUrl,
    token,
    fetchImpl = globalThis.fetch,
    sendBatch,
    capacity = DEFAULT_CAPACITY,
    batchSize = DEFAULT_BATCH_SIZE,
    flushIntervalMs = DEFAULT_FLUSH_INTERVAL_MS,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    maxAttempts = 2,
    dropEventFactory,
  }) {
    if (!sendBatch) {
      this.collectorUrl = normalizeCollectorUrl(collectorUrl);
      if (typeof token !== "string" || token.length < 32) throw new TypeError("invalid collector token");
      if (typeof fetchImpl !== "function") throw new TypeError("fetch implementation is required");
    }
    this.token = token;
    this.fetchImpl = fetchImpl;
    this.sendBatchOverride = sendBatch;
    this.capacity = positiveInteger(capacity, DEFAULT_CAPACITY, 100_000);
    this.batchSize = positiveInteger(batchSize, DEFAULT_BATCH_SIZE, this.capacity);
    this.flushIntervalMs = positiveInteger(flushIntervalMs, DEFAULT_FLUSH_INTERVAL_MS, 60_000);
    this.timeoutMs = positiveInteger(timeoutMs, DEFAULT_TIMEOUT_MS, 60_000);
    this.maxAttempts = positiveInteger(maxAttempts, 2, 5);
    this.dropEventFactory = dropEventFactory;
    this.queue = [];
    this.timer = null;
    this.inFlight = null;
    this.closed = false;
    this.droppedEvents = 0;
    this.sentEvents = 0;
    this.failedBatches = 0;
    this.maxObservedQueue = 0;
  }

  enqueue(event) {
    if (this.closed) return false;
    if (this.queue.length >= this.capacity) {
      this.queue.shift();
      this.droppedEvents += 1;
    }
    this.queue.push({ event, attempt: 0 });
    this.maxObservedQueue = Math.max(this.maxObservedQueue, this.queue.length);
    this.#schedule(this.flushIntervalMs);
    return true;
  }

  #schedule(milliseconds) {
    if (this.closed || this.timer || this.inFlight || this.queue.length === 0) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.#drainOnce();
    }, milliseconds);
    this.timer.unref?.();
  }

  async #post(events) {
    if (this.sendBatchOverride) {
      await this.sendBatchOverride(events, { timeoutMs: this.timeoutMs });
      return;
    }
    const controller = new AbortController();
    let timeout;
    const timeoutPromise = new Promise((_, reject) => {
      timeout = setTimeout(() => {
        controller.abort();
        reject(new Error("collector request timed out"));
      }, this.timeoutMs);
    });
    try {
      const response = await Promise.race([
        this.fetchImpl(this.collectorUrl, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${this.token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(events),
          signal: controller.signal,
        }),
        timeoutPromise,
      ]);
      if (!response?.ok) throw new Error("collector rejected telemetry");
      await response.body?.cancel?.();
    } finally {
      clearTimeout(timeout);
      controller.abort();
    }
  }

  #drainOnce() {
    if (this.closed || this.inFlight || this.queue.length === 0) return this.inFlight;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }

    const entries = this.queue.splice(0, this.batchSize);
    const reportedDrops = this.droppedEvents;
    const events = entries.map((entry) => entry.event);
    if (reportedDrops > 0 && typeof this.dropEventFactory === "function") {
      events.unshift(this.dropEventFactory(reportedDrops, this.queue.length));
    }

    this.inFlight = this.#post(events)
      .then(() => {
        this.sentEvents += events.length;
        this.droppedEvents = Math.max(0, this.droppedEvents - reportedDrops);
      })
      .catch(() => {
        this.failedBatches += 1;
        const retry = [];
        for (const entry of entries) {
          entry.attempt += 1;
          if (entry.attempt < this.maxAttempts) retry.push(entry);
          else this.droppedEvents += 1;
        }
        const available = Math.max(0, this.capacity - this.queue.length);
        const acceptedRetry = retry.slice(0, available);
        this.droppedEvents += retry.length - acceptedRetry.length;
        this.queue.unshift(...acceptedRetry);
      })
      .finally(() => {
        this.inFlight = null;
        if (this.queue.length > 0) this.#schedule(this.failedBatches ? this.flushIntervalMs * 2 : 0);
      });
    return this.inFlight;
  }

  async flush({ deadlineMs = 50 } = {}) {
    const deadline = Date.now() + Math.max(0, deadlineMs);
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    while (!this.closed && (this.queue.length > 0 || this.inFlight)) {
      const current = this.inFlight ?? this.#drainOnce();
      const remaining = deadline - Date.now();
      if (!current || remaining <= 0) break;
      await settleWithin(current, remaining);
      if (Date.now() >= deadline) break;
    }
    return this.diagnostics();
  }

  async close({ deadlineMs = 50 } = {}) {
    await this.flush({ deadlineMs });
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  diagnostics() {
    return {
      queueSize: this.queue.length,
      capacity: this.capacity,
      droppedEvents: this.droppedEvents,
      sentEvents: this.sentEvents,
      failedBatches: this.failedBatches,
      maxObservedQueue: this.maxObservedQueue,
      inFlight: Boolean(this.inFlight),
      closed: this.closed,
    };
  }
}

export function createTelemetryTransport(config) {
  return new TelemetryTransport(config);
}
