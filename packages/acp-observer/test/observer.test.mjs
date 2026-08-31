import assert from "node:assert/strict";
import test from "node:test";

import { createAcpObserver } from "../src/index.js";
import { baseConfig, createClock, RecordingTransport } from "./helpers.mjs";

function setup(overrides = {}) {
  const clock = createClock();
  const transport = new RecordingTransport();
  const observer = createAcpObserver(baseConfig({ testClock: clock, transport, ...overrides }));
  return { clock, transport, observer };
}

function establishSession(observer, clock, metadata = []) {
  observer.observeClientMessage(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "session/new",
      params: { cwd: "/not-observed", mcpServers: [{ env: metadata }] },
    },
    clock.now(),
  );
  clock.set(10);
  observer.observeServerMessage(
    { jsonrpc: "2.0", id: 1, result: { sessionId: "raw-session-secret" } },
    clock.now(),
  );
}

test("observer emits deterministic content-free turn, tool, usage, and latency events", () => {
  const { clock, transport, observer } = setup();
  const privateValue = "private-material-that-must-not-appear";
  establishSession(observer, clock, [
    { name: "BUZZ_ACP_DISPLAY_NAME", value: "Implementor 02" },
    { name: "BUZZ_PRIVATE_KEY", value: privateValue },
    { name: "BUZZ_AUTH_TAG", value: "shared-owner-tag" },
  ]);

  const prompt = {
    jsonrpc: "2.0",
    id: 2,
    method: "session/prompt",
    params: {
      sessionId: "raw-session-secret",
      prompt: [{ type: "text", text: "sensitive prompt body" }],
    },
  };
  const promptSnapshot = structuredClone(prompt);
  clock.set(100);
  observer.observeClientMessage(prompt, clock.now());
  assert.deepEqual(prompt, promptSnapshot, "observer must not mutate protocol messages");

  clock.set(120);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "" } },
    },
  }, clock.now());

  clock.set(150);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: { sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "private reasoning" } },
    },
  }, clock.now());

  clock.set(260);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: {
        sessionUpdate: "tool_call",
        toolCallId: "raw-tool-id",
        title: "Read a private path",
        kind: "read",
        status: "in_progress",
        rawInput: { path: "/not-observed" },
      },
    },
  }, clock.now());

  clock.set(280);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: {
        sessionUpdate: "tool_call_update",
        toolCallId: "raw-tool-id",
        status: "completed",
        rawOutput: "private tool result",
      },
    },
  }, clock.now());

  clock.set(300);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "visible private answer" } },
    },
  }, clock.now());

  clock.set(320);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: { sessionUpdate: "usage_update", used: 1234, size: 32_768 },
    },
  }, clock.now());

  clock.set(500);
  observer.observeServerMessage({ jsonrpc: "2.0", id: 2, result: { stopReason: "end_turn" } }, clock.now());

  const types = transport.events.map((event) => event.event_type);
  assert.deepEqual(types, [
    "process.started",
    "session.started",
    "turn.started",
    "turn.first_activity",
    "turn.stall",
    "turn.first_tool",
    "tool.started",
    "tool.completed",
    "turn.first_visible_text",
    "usage.updated",
    "turn.completed",
  ]);
  const completed = transport.events.find((event) => event.event_type === "turn.completed");
  assert.equal(completed.attributes.duration_ms, 400);
  assert.equal(completed.attributes.ttfa_ms, 50);
  assert.equal(completed.attributes.ttfvt_ms, 200);
  assert.equal(completed.attributes.first_tool_ms, 160);
  assert.equal(completed.attributes.tool_count, 1);

  const serialized = JSON.stringify(transport.events);
  for (const forbidden of [
    "sensitive prompt body",
    "private reasoning",
    "visible private answer",
    "private tool result",
    "/not-observed",
    "raw-session-secret",
    "raw-tool-id",
    privateValue,
    "shared-owner-tag",
  ]) {
    assert.doesNotMatch(serialized, new RegExp(forbidden.replaceAll("/", "\\/"), "u"));
  }
  assert.match(serialized, /Implementor 02/u);
});

test("cancellation and JSON-RPC failure produce exactly one terminal turn event", () => {
  const { clock, transport, observer } = setup();
  establishSession(observer, clock);

  clock.set(100);
  observer.observeClientMessage({ id: "a", method: "session/prompt", params: { sessionId: "raw-session-secret" } }, 100);
  observer.observeClientMessage({ method: "session/cancel", params: { sessionId: "raw-session-secret" } }, 150);
  observer.observeServerMessage({ id: "a", result: { stopReason: "cancelled" } }, 200);

  clock.set(300);
  observer.observeClientMessage({ id: "b", method: "session/prompt", params: { sessionId: "raw-session-secret" } }, 300);
  observer.observeServerMessage({ id: "b", error: { code: -32_603, message: "sensitive backend error" } }, 400);

  assert.equal(transport.events.filter((event) => event.event_type === "turn.cancelled").length, 1);
  assert.equal(transport.events.filter((event) => event.event_type === "turn.failed").length, 1);
  assert.doesNotMatch(JSON.stringify(transport.events), /sensitive backend error/u);
});

test("replayed and background updates without an active prompt are ignored", () => {
  const { clock, transport, observer } = setup();
  establishSession(observer, clock);
  observer.observeServerMessage({
    method: "session/update",
    params: {
      sessionId: "raw-session-secret",
      update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "replayed" } },
    },
  }, 50);
  assert.deepEqual(transport.events.map((event) => event.event_type), ["process.started", "session.started"]);
});

test("observer state is bounded and malformed inputs never throw", () => {
  const { clock, observer } = setup({ maxSessions: 2, maxPendingRequests: 2, maxToolsPerTurn: 2 });
  assert.doesNotThrow(() => observer.observeClientMessage(null));
  assert.doesNotThrow(() => observer.observeServerMessage("not-an-object"));
  for (let index = 0; index < 10; index += 1) {
    observer.observeClientMessage({ id: index, method: "session/new", params: {} }, clock.now());
    observer.observeServerMessage({ id: index, result: { sessionId: `session-${index}` } }, clock.now());
  }
  assert.ok(observer.diagnostics().sessions <= 2);
  assert.ok(observer.diagnostics().activeTurns <= 2);
});

test("transport failures are isolated from the protocol path", async () => {
  const transport = new RecordingTransport({ throwOnEnqueue: true });
  const observer = createAcpObserver(baseConfig({ transport }));
  assert.doesNotThrow(() => observer.observeClientMessage({ id: 1, method: "session/new", params: {} }));
  assert.doesNotThrow(() => observer.observeProtocolAnomaly({ kind: "malformed_json", lineBytes: 20 }));
  assert.doesNotThrow(() => observer.observeProcessExit({ code: 1 }));
  await assert.doesNotReject(observer.flush({ deadlineMs: 5 }));
  assert.ok(observer.diagnostics().observerErrors > 0);
});

test("disabled observer is a complete no-op", async () => {
  const observer = createAcpObserver({ enabled: false });
  observer.observeClientMessage({});
  observer.observeServerMessage({});
  observer.observeProtocolAnomaly({});
  observer.observeProcessExit({});
  await observer.flush();
  assert.equal(observer.diagnostics().enabled, false);
});
