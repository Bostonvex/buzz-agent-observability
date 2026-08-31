import { createHmac } from "node:crypto";

const IDENTITY_KEYS = [
  "BUZZ_TELEMETRY_AGENT_ID",
  "BUZZ_ACP_DISPLAY_NAME",
  "BUZZ_GIT_ORIGIN_AGENT_NAME",
];

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/u;
const SECRET_LIKE = [
  new RegExp("AK" + "IA[0-9A-Z]{16}"),
  new RegExp("gh" + "[pousr]_[A-Za-z0-9_]{20,}"),
  new RegExp("s" + "k-[A-Za-z0-9_-]{20,}"),
  new RegExp("xo" + "x[baprs]-[A-Za-z0-9-]{10,}"),
  new RegExp("-----BEGIN " + "(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
];

export function hmacIdentifier(salt, namespace, value) {
  const key = Buffer.isBuffer(salt) ? salt : Buffer.from(String(salt), "utf8");
  const digest = createHmac("sha256", key)
    .update(`${namespace}\0${String(value)}`, "utf8")
    .digest("base64url")
    .slice(0, 30);
  return `h_${digest}`;
}

export function safeLabel(value, fallback, maximum = 128) {
  if (typeof value !== "string") return fallback;
  const normalized = value.trim();
  if (!normalized || normalized.length > maximum || CONTROL_CHARACTERS.test(normalized)) {
    return fallback;
  }
  if (SECRET_LIKE.some((pattern) => pattern.test(normalized))) return fallback;
  return normalized;
}

function approvedEnvironmentFromSession(message) {
  const approved = {};
  const servers = message?.params?.mcpServers;
  if (!Array.isArray(servers)) return approved;

  for (const server of servers) {
    const environment = server?.env;
    if (Array.isArray(environment)) {
      for (const entry of environment) {
        if (!entry || typeof entry.name !== "string") continue;
        if (!IDENTITY_KEYS.includes(entry.name)) continue;
        if (typeof entry.value === "string") approved[entry.name] = entry.value;
      }
      continue;
    }
    if (environment && typeof environment === "object") {
      for (const key of IDENTITY_KEYS) {
        if (typeof environment[key] === "string") approved[key] = environment[key];
      }
    }
  }
  return approved;
}

export function identityMetadataFromSession(message) {
  return approvedEnvironmentFromSession(message);
}

export function resolveIdentity({
  salt,
  sessionId,
  metadata = {},
  explicitId,
  explicitDisplayName,
  mappedIdentity,
}) {
  const sessionHash = hmacIdentifier(salt, "session", sessionId);
  const mapped = typeof mappedIdentity === "function" ? mappedIdentity(sessionHash) : null;
  const mappedId = typeof mapped === "string" ? mapped : mapped?.id;
  const mappedDisplayName = typeof mapped === "object" ? mapped?.displayName : null;

  const identityMaterial =
    safeLabel(explicitId, null) ??
    safeLabel(explicitDisplayName, null) ??
    safeLabel(metadata.BUZZ_TELEMETRY_AGENT_ID, null) ??
    safeLabel(metadata.BUZZ_ACP_DISPLAY_NAME, null) ??
    safeLabel(metadata.BUZZ_GIT_ORIGIN_AGENT_NAME, null) ??
    safeLabel(mappedId, null) ??
    `session:${sessionId}`;
  const displayName =
    safeLabel(explicitDisplayName, null) ??
    safeLabel(metadata.BUZZ_ACP_DISPLAY_NAME, null) ??
    safeLabel(metadata.BUZZ_GIT_ORIGIN_AGENT_NAME, null) ??
    safeLabel(mappedDisplayName, null) ??
    `Unknown agent ${sessionHash.slice(0, 8)}`;

  return {
    agent: {
      id: hmacIdentifier(salt, "agent", identityMaterial),
      display_name: displayName,
    },
    sessionId: sessionHash,
  };
}
