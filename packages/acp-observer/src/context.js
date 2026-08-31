const DEFAULT_CAPACITY = 256;
const DEFAULT_TIMEOUT_MS = 100;

function isLoopbackHostname(hostname) {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
}

export function normalizeModelProxyContextUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "http:" || !isLoopbackHostname(url.hostname)) {
    throw new TypeError("model proxy context URL must use HTTP on a loopback hostname");
  }
  if (
    url.pathname !== "/__buzz/context" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new TypeError("model proxy context URL must be the plain /__buzz/context endpoint");
  }
  return url.toString();
}

function positiveInteger(value, fallback, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 1) return fallback;
  return Math.min(maximum, Math.floor(number));
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

export class ModelProxyContextSink {
  constructor({
    contextUrl,
    token,
    fetchImpl = globalThis.fetch,
    capacity = DEFAULT_CAPACITY,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  }) {
    this.contextUrl = normalizeModelProxyContextUrl(contextUrl);
    if (typeof token !== "string" || token.length < 32) throw new TypeError("invalid context token");
    if (typeof fetchImpl !== "function") throw new TypeError("fetch implementation is required");
    this.token = token;
    this.fetchImpl = fetchImpl;
    this.capacity = positiveInteger(capacity, DEFAULT_CAPACITY, 10_000);
    this.timeoutMs = positiveInteger(timeoutMs, DEFAULT_TIMEOUT_MS, 5_000);
    this.queue = [];
    this.inFlight = null;
    this.closed = false;
    this.dropped = 0;
  }

  start(context) {
    return this.#enqueue({ action: "start", context });
  }

  end(contextId) {
    return this.#enqueue({ action: "end", context_id: contextId });
  }

  #enqueue(item) {
    if (this.closed) return false;
    if (this.queue.length >= this.capacity) {
      this.dropped += 1;
      return false;
    }
    this.queue.push(item);
    this.#drain();
    return true;
  }

  #drain() {
    if (this.closed || this.inFlight || this.queue.length === 0) return this.inFlight;
    const item = this.queue.shift();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    timeout.unref?.();
    this.inFlight = Promise.resolve()
      .then(() =>
        this.fetchImpl(this.contextUrl, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${this.token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(item),
          signal: controller.signal,
        }),
      )
      .then((response) => {
        if (!response?.ok) throw new Error("model proxy rejected context");
        return response.body?.cancel?.();
      })
      .catch(() => {
        this.dropped += 1;
      })
      .finally(() => {
        clearTimeout(timeout);
        controller.abort();
        this.inFlight = null;
        this.#drain();
      });
    return this.inFlight;
  }

  async flush({ deadlineMs = 50 } = {}) {
    const deadline = Date.now() + Math.max(0, deadlineMs);
    while (!this.closed && (this.queue.length > 0 || this.inFlight)) {
      const current = this.inFlight ?? this.#drain();
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
  }

  diagnostics() {
    return {
      queueSize: this.queue.length,
      dropped: this.dropped,
      inFlight: Boolean(this.inFlight),
      closed: this.closed,
    };
  }
}

export function createModelProxyContextSink(config) {
  return new ModelProxyContextSink(config);
}
