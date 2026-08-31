import { lstatSync, readFileSync } from "node:fs";

import { createAcpObserver } from "./observer.js";
import { createModelProxyContextSink } from "./context.js";

export {
  createModelProxyContextSink,
  ModelProxyContextSink,
  normalizeModelProxyContextUrl,
} from "./context.js";
export { hmacIdentifier, identityMetadataFromSession, resolveIdentity, safeLabel } from "./identity.js";
export { createAcpObserver } from "./observer.js";
export { createTelemetryTransport, normalizeCollectorUrl, TelemetryTransport } from "./transport.js";

function enabled(value) {
  return value === "1" || value?.toLowerCase?.() === "true";
}

function readPrivateFile(path, minimumLength) {
  if (typeof path !== "string" || !path) throw new TypeError("private file path is required");
  const details = lstatSync(path);
  if (!details.isFile() || details.isSymbolicLink() || (details.mode & 0o077) !== 0) {
    throw new TypeError("private file must be regular, non-symlinked, and mode 0600 or stricter");
  }
  const value = readFileSync(path, "utf8").trim();
  if (value.length < minimumLength || value.length > 512) throw new TypeError("private file value is invalid");
  return value;
}

export function createAcpObserverFromEnv(overrides = {}, environment = process.env) {
  if (!enabled(environment.BUZZ_TELEMETRY_ENABLED)) return createAcpObserver({ enabled: false });
  try {
    const token = readPrivateFile(environment.BUZZ_TELEMETRY_TOKEN_FILE, 32);
    const identitySalt = readPrivateFile(environment.BUZZ_TELEMETRY_IDENTITY_SALT_FILE, 16);
    let contextSink;
    if (environment.BUZZ_MODEL_PROXY_CONTEXT_URL) {
      try {
        contextSink = createModelProxyContextSink({
          contextUrl: environment.BUZZ_MODEL_PROXY_CONTEXT_URL,
          token,
          timeoutMs: Number(environment.BUZZ_MODEL_PROXY_CONTEXT_TIMEOUT_MS ?? 100),
        });
      } catch {
        contextSink = undefined;
      }
    }
    return createAcpObserver({
      enabled: true,
      harness: overrides.harness,
      harnessVersion: overrides.harnessVersion,
      model: overrides.model,
      endpointId: environment.BUZZ_TELEMETRY_ENDPOINT_ID ?? overrides.endpointId,
      producerName: overrides.producerName,
      producerVersion: overrides.producerVersion,
      agentTelemetryId: environment.BUZZ_TELEMETRY_AGENT_ID,
      agentDisplayName:
        environment.BUZZ_ACP_DISPLAY_NAME ?? environment.BUZZ_GIT_ORIGIN_AGENT_NAME,
      identitySalt,
      contextSink,
      transportOptions: {
        collectorUrl:
          environment.BUZZ_TELEMETRY_URL ?? "http://127.0.0.1:7900/api/v1/events",
        token,
        capacity: Number(environment.BUZZ_TELEMETRY_QUEUE_CAPACITY ?? 1024),
        batchSize: Number(environment.BUZZ_TELEMETRY_BATCH_SIZE ?? 50),
        flushIntervalMs: Number(environment.BUZZ_TELEMETRY_FLUSH_INTERVAL_MS ?? 250),
        timeoutMs: Number(environment.BUZZ_TELEMETRY_TIMEOUT_MS ?? 200),
      },
      stallThresholdMs: Number(environment.BUZZ_TELEMETRY_STALL_THRESHOLD_MS ?? 15_000),
    });
  } catch {
    return createAcpObserver({ enabled: false });
  }
}
