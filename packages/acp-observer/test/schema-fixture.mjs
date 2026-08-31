import { createAcpObserver } from "../src/index.js";
import { baseConfig, createClock, RecordingTransport } from "./helpers.mjs";

const clock = createClock();
const transport = new RecordingTransport();
const observer = createAcpObserver(baseConfig({ testClock: clock, transport }));

observer.observeClientMessage({
  id: 1,
  method: "session/new",
  params: {
    mcpServers: [
      { env: [{ name: "BUZZ_ACP_DISPLAY_NAME", value: "Schema fixture agent" }] },
    ],
  },
}, 1);
observer.observeServerMessage({ id: 1, result: { sessionId: "fixture-session" } }, 2);
observer.observeClientMessage({ id: 2, method: "session/prompt", params: { sessionId: "fixture-session" } }, 10);
observer.observeServerMessage({
  method: "session/update",
  params: {
    sessionId: "fixture-session",
    update: { sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "not retained" } },
  },
}, 20);
observer.observeServerMessage({
  method: "session/update",
  params: {
    sessionId: "fixture-session",
    update: { sessionUpdate: "tool_call", toolCallId: "tool-1", kind: "execute", status: "in_progress" },
  },
}, 30);
observer.observeServerMessage({
  method: "session/update",
  params: {
    sessionId: "fixture-session",
    update: { sessionUpdate: "tool_call_update", toolCallId: "tool-1", status: "failed" },
  },
}, 40);
observer.observeServerMessage({
  method: "session/update",
  params: {
    sessionId: "fixture-session",
    update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "not retained" } },
  },
}, 50);
observer.observeServerMessage({
  method: "session/update",
  params: {
    sessionId: "fixture-session",
    update: { sessionUpdate: "usage_update", used: 123, size: 1000 },
  },
}, 60);
observer.observeServerMessage({ id: 2, error: { code: -32_000, message: "not retained" } }, 70);
observer.observeClientMessage({ id: 3, method: "session/prompt", params: { sessionId: "fixture-session" } }, 80);
observer.observeClientMessage({ method: "session/cancel", params: { sessionId: "fixture-session" } }, 90);
observer.observeProtocolAnomaly({ kind: "malformed_json", lineBytes: 55 }, 95);
observer.observeProcessExit({ code: 0 }, 100);

process.stdout.write(JSON.stringify(transport.events));
