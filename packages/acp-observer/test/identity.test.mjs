import assert from "node:assert/strict";
import test from "node:test";

import {
  hmacIdentifier,
  identityMetadataFromSession,
  resolveIdentity,
} from "../src/index.js";

test("identity extraction never reads prohibited ACP environment values", () => {
  const environment = {
    BUZZ_ACP_DISPLAY_NAME: "Implementor 02",
    BUZZ_GIT_ORIGIN_AGENT_NAME: "implementor_02",
  };
  Object.defineProperty(environment, "BUZZ_PRIVATE_KEY", {
    enumerable: true,
    get() {
      throw new Error("private key value was read");
    },
  });
  Object.defineProperty(environment, "BUZZ_AUTH_TAG", {
    enumerable: true,
    get() {
      throw new Error("authorization tag value was read");
    },
  });

  const metadata = identityMetadataFromSession({
    params: { mcpServers: [{ env: environment }] },
  });
  assert.deepEqual(metadata, {
    BUZZ_ACP_DISPLAY_NAME: "Implementor 02",
    BUZZ_GIT_ORIGIN_AGENT_NAME: "implementor_02",
  });
});

test("stable identity is HMAC-derived and friendly name is separate", () => {
  const identity = resolveIdentity({
    salt: "stable-test-salt-1234567890",
    sessionId: "raw-session-value",
    metadata: { BUZZ_GIT_ORIGIN_AGENT_NAME: "reviewer_01" },
  });
  assert.equal(identity.agent.display_name, "reviewer_01");
  assert.equal(identity.agent.id, hmacIdentifier("stable-test-salt-1234567890", "agent", "reviewer_01"));
  assert.equal(identity.sessionId, hmacIdentifier("stable-test-salt-1234567890", "session", "raw-session-value"));
  assert.doesNotMatch(identity.agent.id, /reviewer/u);
  assert.doesNotMatch(identity.sessionId, /raw-session/u);
});

test("unknown identities remain stable per hashed session", () => {
  const first = resolveIdentity({ salt: "stable-test-salt-1234567890", sessionId: "one" });
  const second = resolveIdentity({ salt: "stable-test-salt-1234567890", sessionId: "two" });
  assert.match(first.agent.display_name, /^Unknown agent /u);
  assert.notEqual(first.agent.id, second.agent.id);
  assert.notEqual(first.sessionId, second.sessionId);
});
