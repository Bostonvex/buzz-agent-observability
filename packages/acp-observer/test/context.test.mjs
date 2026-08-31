import assert from "node:assert/strict";
import test from "node:test";

import {
  createModelProxyContextSink,
  normalizeModelProxyContextUrl,
} from "../src/index.js";

test("model proxy context URL accepts only the loopback control endpoint", () => {
  assert.equal(
    normalizeModelProxyContextUrl("http://127.0.0.1:39123/__buzz/context"),
    "http://127.0.0.1:39123/__buzz/context",
  );
  assert.throws(
    () => normalizeModelProxyContextUrl("http://192.0.2.10:39123/__buzz/context"),
    /loopback/u,
  );
  assert.throws(
    () => normalizeModelProxyContextUrl("http://127.0.0.1:39123/v1/chat/completions"),
    /plain/u,
  );
});

test("context sink preserves start/end ordering and never places the token in its body", async () => {
  const requests = [];
  const token = "context-token-" + "x".repeat(32);
  const sink = createModelProxyContextSink({
    contextUrl: "http://127.0.0.1:39123/__buzz/context",
    token,
    fetchImpl: async (_url, options) => {
      requests.push({ body: options.body, authorization: options.headers.Authorization });
      return { ok: true, body: { cancel: async () => {} } };
    },
  });
  const context = {
    context_id: "turn-safe",
    agent_id: "agent-safe",
    display_name: "Agent Safe",
    harness: "deepseek",
    model: "model-safe",
    endpoint_id: "endpoint-safe",
    session_id: "session-safe",
    turn_id: "turn-safe",
  };
  sink.start(context);
  sink.end(context.context_id);
  await sink.flush({ deadlineMs: 500 });

  assert.deepEqual(requests.map(({ body }) => JSON.parse(body).action), ["start", "end"]);
  assert.equal(requests[0].authorization, `Bearer ${token}`);
  assert.equal(requests.some(({ body }) => body.includes(token)), false);
  assert.equal(sink.diagnostics().dropped, 0);
});

test("context sink is bounded and absorbs delivery failures", async () => {
  const sink = createModelProxyContextSink({
    contextUrl: "http://127.0.0.1:39123/__buzz/context",
    token: "x".repeat(40),
    capacity: 2,
    fetchImpl: async () => {
      throw new Error("unavailable");
    },
  });
  assert.doesNotThrow(() => {
    sink.start({ context_id: "one" });
    sink.end("one");
    sink.end("two");
    sink.end("three");
  });
  await assert.doesNotReject(sink.flush({ deadlineMs: 500 }));
  assert.ok(sink.diagnostics().dropped >= 2);
});
