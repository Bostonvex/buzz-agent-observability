import { createAcpObserverFromEnv } from "../src/index.js";

const observer = createAcpObserverFromEnv({
  harness: "deepseek",
  harnessVersion: "0.1.0",
  model: "example-model",
  producerName: "http-fixture",
  producerVersion: "0.1.0",
});

observer.observeClientMessage({ id: 1, method: "session/new", params: {} }, 1);
observer.observeServerMessage({ id: 1, result: { sessionId: "http-fixture-session" } }, 2);
observer.observeClientMessage({
  id: 2,
  method: "session/prompt",
  params: { sessionId: "http-fixture-session", prompt: [{ type: "text", text: "not retained" }] },
}, 10);
observer.observeServerMessage({
  method: "session/update",
  params: {
    sessionId: "http-fixture-session",
    update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "not retained" } },
  },
}, 20);
observer.observeServerMessage({ id: 2, result: { stopReason: "end_turn" } }, 30);
observer.observeProcessExit({ code: 0 }, 40);
await observer.flush({ deadlineMs: 2_000 });
process.stdout.write(JSON.stringify(observer.diagnostics()));
