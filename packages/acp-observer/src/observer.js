import { performance } from "node:perf_hooks";
import { randomUUID } from "node:crypto";

import {
  hmacIdentifier,
  identityMetadataFromSession,
  resolveIdentity,
  safeLabel,
} from "./identity.js";
import { createTelemetryTransport } from "./transport.js";

const MEANINGFUL_UPDATES = new Set([
  "agent_message_chunk",
  "agent_thought_chunk",
  "plan",
  "tool_call",
  "tool_call_update",
  "usage_update",
]);
const TERMINAL_TOOL_STATUSES = new Set(["completed", "failed"]);

function positiveInteger(value, fallback, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 1) return fallback;
  return Math.min(maximum, Math.floor(number));
}

function requestKey(id) {
  try {
    return JSON.stringify(id);
  } catch {
    return String(id);
  }
}

function safeIdentifier(value, fallback = null) {
  const label = safeLabel(value, fallback, 256);
  if (label === null) return null;
  return /^[A-Za-z0-9][A-Za-z0-9._:/+@=-]{0,255}$/u.test(label) ? label : fallback;
}

function finiteNonNegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function hasNonEmptyText(update) {
  return (
    update?.sessionUpdate === "agent_message_chunk" &&
    update?.content?.type === "text" &&
    typeof update.content.text === "string" &&
    update.content.text.length > 0
  );
}

function measurementAttributes(turn, now) {
  const attributes = {
    duration_ms: Math.max(0, now - turn.startedAt),
    max_stall_ms: turn.maxStallMs,
    tool_count: turn.toolCount,
    measurement_quality: "exact",
  };
  if (turn.firstActivityAt !== null) attributes.ttfa_ms = turn.firstActivityAt - turn.startedAt;
  if (turn.firstVisibleTextAt !== null) attributes.ttfvt_ms = turn.firstVisibleTextAt - turn.startedAt;
  if (turn.firstToolAt !== null) attributes.first_tool_ms = turn.firstToolAt - turn.startedAt;
  return attributes;
}

class AcpObserver {
  constructor(config) {
    this.enabled = config.enabled !== false;
    this.clock = config.clock ?? (() => performance.now());
    this.wallClock = config.wallClock ?? (() => new Date());
    this.uuid = config.randomUUID ?? randomUUID;
    this.salt = config.identitySalt;
    this.harness = safeIdentifier(config.harness);
    this.model = safeIdentifier(config.model);
    this.endpointId = safeIdentifier(config.endpointId);
    this.producer = {
      name: safeIdentifier(config.producerName, "acp-observer"),
      version: safeIdentifier(config.producerVersion, "0.1.0"),
      instance_id: safeIdentifier(config.instanceId, this.uuid()),
    };
    this.explicitAgentId = safeLabel(config.agentTelemetryId, null);
    this.explicitDisplayName = safeLabel(config.agentDisplayName, null);
    this.identityMapping = config.identityMapping;
    this.stallThresholdMs = positiveInteger(config.stallThresholdMs, 15_000, 3_600_000);
    this.maxSessions = positiveInteger(config.maxSessions, 256, 10_000);
    this.maxTurns = positiveInteger(config.maxTurns, 1024, 100_000);
    this.maxToolsPerTurn = positiveInteger(config.maxToolsPerTurn, 128, 10_000);
    this.maxPendingRequests = positiveInteger(
      config.maxPendingRequests,
      this.maxSessions + this.maxTurns,
      100_000,
    );
    this.sessions = new Map();
    this.pendingRequests = new Map();
    this.turns = new Map();
    this.observerErrors = 0;
    this.processExited = false;
    this.processStartedAt = this.clock();
    this.processStartedEmitted = false;

    const processIdentity = resolveIdentity({
      salt: this.salt,
      sessionId: `process:${this.producer.instance_id}`,
      explicitId: this.explicitAgentId,
      explicitDisplayName: this.explicitDisplayName,
    });
    this.processAgent = processIdentity.agent;
    this.processSessionId = processIdentity.sessionId;

    this.transport =
      config.transport ??
      createTelemetryTransport({
        ...config.transportOptions,
        dropEventFactory: (count, queueDepth) =>
          this.#makeEvent(
            "collector.dropped_events",
            {
              agent: this.processAgent,
              sessionId: null,
              turnId: null,
              spanId: null,
              parentSpanId: null,
            },
            { dropped_count: count, queue_depth: queueDepth, measurement_quality: "derived" },
            this.clock(),
          ),
      });

    this.harnessVersion = safeIdentifier(config.harnessVersion, "unknown");
    if (this.explicitAgentId || this.explicitDisplayName) this.#ensureProcessStarted();
  }

  #makeEvent(eventType, context, attributes, now) {
    return {
      schema_version: 1,
      event_id: this.uuid(),
      event_type: eventType,
      observed_at: this.wallClock().toISOString(),
      monotonic_offset_ms: Math.max(0, now),
      producer: this.producer,
      agent: context.agent,
      harness: this.harness,
      model: this.model,
      endpoint_id: this.endpointId,
      session_id: context.sessionId,
      turn_id: context.turnId,
      span_id: context.spanId,
      parent_span_id: context.parentSpanId,
      attributes,
    };
  }

  #emit(eventType, context, attributes, now) {
    try {
      this.transport.enqueue(this.#makeEvent(eventType, context, attributes, now));
    } catch {
      this.observerErrors += 1;
    }
  }

  #ensureProcessStarted(agent = null) {
    if (this.processStartedEmitted) return;
    if (agent) this.processAgent = agent;
    this.processStartedEmitted = true;
    this.#emit(
      "process.started",
      {
        agent: this.processAgent,
        sessionId: null,
        turnId: null,
        spanId: null,
        parentSpanId: null,
      },
      { harness_version: this.harnessVersion },
      this.processStartedAt,
    );
  }

  #safe(action) {
    if (!this.enabled || this.processExited) return;
    try {
      action();
    } catch {
      this.observerErrors += 1;
    }
  }

  #context(session, turn = null, tool = null) {
    return {
      agent: session?.agent ?? this.processAgent,
      sessionId: session?.hashedSessionId ?? null,
      turnId: turn?.turnId ?? null,
      spanId: tool?.spanId ?? null,
      parentSpanId: tool ? turn?.turnId ?? null : null,
    };
  }

  #ensureSession(rawSessionId, metadata = {}, now = this.clock()) {
    if (typeof rawSessionId !== "string" || !rawSessionId) return null;
    let session = this.sessions.get(rawSessionId);
    if (session) {
      session.lastSeenAt = now;
      return session;
    }
    while (this.sessions.size >= this.maxSessions) {
      const [oldestId, oldest] = this.sessions.entries().next().value ?? [];
      if (!oldestId) break;
      for (const key of oldest.activeTurnKeys) this.#finishTurn(this.turns.get(key), "failed", now, "observer_capacity");
      this.sessions.delete(oldestId);
    }
    const identity = resolveIdentity({
      salt: this.salt,
      sessionId: rawSessionId,
      metadata,
      explicitId: this.explicitAgentId,
      explicitDisplayName: this.explicitDisplayName,
      mappedIdentity: this.identityMapping,
    });
    session = {
      rawSessionId,
      hashedSessionId: identity.sessionId,
      agent: identity.agent,
      startedAt: now,
      lastSeenAt: now,
      activeTurnKeys: new Set(),
      currentTurnKey: null,
    };
    this.sessions.set(rawSessionId, session);
    this.#ensureProcessStarted(session.agent);
    this.#emit("session.started", this.#context(session), {}, now);
    return session;
  }

  #startTurn(session, key, now) {
    if (!session) return;
    while (this.turns.size >= this.maxTurns) {
      const oldest = this.turns.values().next().value;
      if (!oldest) break;
      this.#finishTurn(oldest, "failed", now, "observer_capacity");
    }
    const previous = session.currentTurnKey ? this.turns.get(session.currentTurnKey) : null;
    if (previous && !previous.ended) this.#finishTurn(previous, "cancelled", now);
    const turn = {
      key,
      turnId: this.uuid(),
      session,
      startedAt: now,
      lastUpdateAt: now,
      firstActivityAt: null,
      firstVisibleTextAt: null,
      firstToolAt: null,
      maxStallMs: 0,
      toolCount: 0,
      tools: new Map(),
      ended: false,
    };
    this.turns.set(key, turn);
    session.activeTurnKeys.add(key);
    session.currentTurnKey = key;
    this.#emit("turn.started", this.#context(session, turn), {}, now);
  }

  #rememberRequest(key, pending) {
    if (this.pendingRequests.has(key)) this.pendingRequests.delete(key);
    while (this.pendingRequests.size >= this.maxPendingRequests) {
      const oldestKey = this.pendingRequests.keys().next().value;
      if (oldestKey === undefined) break;
      const turn = this.turns.get(oldestKey);
      if (turn) this.#finishTurn(turn, "failed", this.clock(), "observer_capacity");
      this.pendingRequests.delete(oldestKey);
    }
    this.pendingRequests.set(key, pending);
  }

  #activeTurn(session) {
    if (!session?.currentTurnKey) return null;
    const turn = this.turns.get(session.currentTurnKey);
    return turn && !turn.ended ? turn : null;
  }

  #observeActivity(turn, updateKind, now) {
    const gap = Math.max(0, now - turn.lastUpdateAt);
    turn.lastUpdateAt = now;
    turn.maxStallMs = Math.max(turn.maxStallMs, gap);
    if (gap >= this.stallThresholdMs) {
      this.#emit(
        "turn.stall",
        this.#context(turn.session, turn),
        {
          elapsed_ms: now - turn.startedAt,
          gap_ms: gap,
          threshold_ms: this.stallThresholdMs,
          measurement_quality: "exact",
        },
        now,
      );
    }
    if (turn.firstActivityAt === null) {
      turn.firstActivityAt = now;
      this.#emit(
        "turn.first_activity",
        this.#context(turn.session, turn),
        {
          elapsed_ms: now - turn.startedAt,
          update_kind: safeLabel(updateKind, "unknown_update"),
          measurement_quality: "exact",
        },
        now,
      );
    }
  }

  #observeTool(turn, update, now) {
    const rawToolId = update?.toolCallId;
    if (typeof rawToolId !== "string" && typeof rawToolId !== "number") return;
    const key = String(rawToolId);
    let tool = turn.tools.get(key);
    if (!tool) {
      if (turn.tools.size >= this.maxToolsPerTurn) {
        this.#emit(
          "protocol.anomaly",
          this.#context(turn.session, turn),
          { anomaly_kind: "tool_capacity", line_bytes: 0, measurement_quality: "derived" },
          now,
        );
        return;
      }
      tool = {
        spanId: this.uuid(),
        startedAt: now,
        kind: safeLabel(update.kind, "other"),
        ended: false,
      };
      turn.tools.set(key, tool);
      turn.toolCount += 1;
      if (turn.firstToolAt === null) {
        turn.firstToolAt = now;
        this.#emit(
          "turn.first_tool",
          this.#context(turn.session, turn),
          { elapsed_ms: now - turn.startedAt, tool_kind: tool.kind, measurement_quality: "exact" },
          now,
        );
      }
      this.#emit(
        "tool.started",
        this.#context(turn.session, turn, tool),
        {
          tool_kind: tool.kind,
          status: safeLabel(update.status, "in_progress"),
          measurement_quality: update.sessionUpdate === "tool_call" ? "exact" : "derived",
        },
        now,
      );
    }

    const status = safeLabel(update.status, "in_progress");
    if (TERMINAL_TOOL_STATUSES.has(status) && !tool.ended) {
      tool.ended = true;
      this.#emit(
        status === "failed" ? "tool.failed" : "tool.completed",
        this.#context(turn.session, turn, tool),
        {
          tool_kind: tool.kind,
          status,
          duration_ms: Math.max(0, now - tool.startedAt),
          measurement_quality: "exact",
          ...(status === "failed" ? { error_category: "tool_failure", error_code: "acp_tool_failed" } : {}),
        },
        now,
      );
    } else if (update.sessionUpdate === "tool_call_update" && !tool.ended) {
      this.#emit(
        "tool.updated",
        this.#context(turn.session, turn, tool),
        {
          tool_kind: tool.kind,
          status,
          elapsed_ms: Math.max(0, now - tool.startedAt),
          measurement_quality: "exact",
        },
        now,
      );
    }
  }

  #finishTurn(turn, outcome, now, errorCode = null) {
    if (!turn || turn.ended) return;
    turn.ended = true;
    const attributes = measurementAttributes(turn, now);
    let eventType = "turn.completed";
    if (outcome === "cancelled") eventType = "turn.cancelled";
    if (outcome === "failed") {
      eventType = "turn.failed";
      attributes.error_category = "protocol_or_process_failure";
      attributes.error_code = safeIdentifier(errorCode, "unknown_failure");
    }
    if (eventType === "turn.completed") attributes.outcome = "completed";
    this.#emit(eventType, this.#context(turn.session, turn), attributes, now);
    turn.session.activeTurnKeys.delete(turn.key);
    if (turn.session.currentTurnKey === turn.key) turn.session.currentTurnKey = null;
    this.turns.delete(turn.key);
    this.pendingRequests.delete(turn.key);
  }

  observeClientMessage(message, monotonicNow = this.clock()) {
    this.#safe(() => {
      if (!message || typeof message !== "object") return;
      const method = message.method;
      const hasId = Object.hasOwn(message, "id");
      const key = hasId ? requestKey(message.id) : `notification:${this.uuid()}`;

      if (method === "session/new") {
        if (hasId) {
          this.#rememberRequest(key, {
            method,
            metadata: identityMetadataFromSession(message),
            requestedAt: monotonicNow,
          });
        }
        return;
      }
      if (method === "session/load" || method === "session/resume") {
        const session = this.#ensureSession(message?.params?.sessionId, {}, monotonicNow);
        if (hasId) this.#rememberRequest(key, { method, session, requestedAt: monotonicNow });
        return;
      }
      if (method === "session/prompt") {
        const session = this.#ensureSession(message?.params?.sessionId, {}, monotonicNow);
        if (!session) return;
        this.#rememberRequest(key, { method, session, requestedAt: monotonicNow });
        this.#startTurn(session, key, monotonicNow);
        return;
      }
      if (method === "session/cancel") {
        const session = this.sessions.get(message?.params?.sessionId);
        if (!session) return;
        for (const turnKey of [...session.activeTurnKeys]) {
          this.#finishTurn(this.turns.get(turnKey), "cancelled", monotonicNow);
        }
      }
    });
  }

  observeServerMessage(message, monotonicNow = this.clock()) {
    this.#safe(() => {
      if (!message || typeof message !== "object") return;
      if (Object.hasOwn(message, "id")) {
        const key = requestKey(message.id);
        const pending = this.pendingRequests.get(key);
        if (!pending) return;
        if (pending.method === "session/new") {
          const rawSessionId = message?.result?.sessionId;
          if (typeof rawSessionId === "string") {
            this.#ensureSession(rawSessionId, pending.metadata, monotonicNow);
          }
          this.pendingRequests.delete(key);
          return;
        }
        if (pending.method === "session/prompt") {
          const turn = this.turns.get(key);
          const stopReason = message?.result?.stopReason;
          if (message.error) {
            const code = finiteNonNegative(Math.abs(Number(message.error.code)));
            this.#finishTurn(turn, "failed", monotonicNow, code === null ? "json_rpc_error" : `json_rpc_${code}`);
          } else if (stopReason === "cancelled") {
            this.#finishTurn(turn, "cancelled", monotonicNow);
          } else {
            this.#finishTurn(turn, "completed", monotonicNow);
          }
          return;
        }
        this.pendingRequests.delete(key);
        return;
      }

      if (message.method !== "session/update") return;
      const rawSessionId = message?.params?.sessionId;
      const session = this.sessions.get(rawSessionId);
      const turn = this.#activeTurn(session);
      const update = message?.params?.update;
      const updateKind = update?.sessionUpdate;
      if (!turn || typeof updateKind !== "string") return;

      const meaningful = MEANINGFUL_UPDATES.has(updateKind) &&
        !(updateKind === "agent_message_chunk" && !hasNonEmptyText(update));
      if (meaningful) this.#observeActivity(turn, updateKind, monotonicNow);

      if (hasNonEmptyText(update) && turn.firstVisibleTextAt === null) {
        turn.firstVisibleTextAt = monotonicNow;
        this.#emit(
          "turn.first_visible_text",
          this.#context(session, turn),
          { elapsed_ms: monotonicNow - turn.startedAt, measurement_quality: "exact" },
          monotonicNow,
        );
      }
      if (updateKind === "tool_call" || updateKind === "tool_call_update") {
        this.#observeTool(turn, update, monotonicNow);
      }
      if (updateKind === "usage_update") {
        const used = finiteNonNegative(update.used);
        if (used !== null) {
          this.#emit(
            "usage.updated",
            this.#context(session, turn),
            {
              token_kind: "context_occupancy",
              value: used,
              semantics: "acp_usage_update_used",
              measurement_quality: "exact",
            },
            monotonicNow,
          );
        }
      }
    });
  }

  observeProcessExit(details = {}, monotonicNow = this.clock()) {
    if (!this.enabled || this.processExited) return;
    try {
      this.#ensureProcessStarted();
      for (const turn of [...this.turns.values()]) {
        this.#finishTurn(turn, "failed", monotonicNow, "process_exit");
      }
      for (const session of this.sessions.values()) {
        this.#emit(
          "session.ended",
          this.#context(session),
          {
            duration_ms: Math.max(0, monotonicNow - session.startedAt),
            outcome: "process_exit",
            measurement_quality: "exact",
          },
          monotonicNow,
        );
      }
      const attributes = {};
      const exitCode = finiteNonNegative(details.code);
      if (exitCode !== null) attributes.exit_code = exitCode;
      if (typeof details.signal === "string") attributes.signal = safeLabel(details.signal, "unknown");
      attributes.outcome = exitCode === 0 ? "completed" : "failed";
      this.#emit(
        "process.exited",
        {
          agent: this.processAgent,
          sessionId: null,
          turnId: null,
          spanId: null,
          parentSpanId: null,
        },
        attributes,
        monotonicNow,
      );
    } catch {
      this.observerErrors += 1;
    } finally {
      this.processExited = true;
    }
  }

  observeProtocolAnomaly(details = {}, monotonicNow = this.clock()) {
    this.#safe(() => {
      this.#ensureProcessStarted();
      const lineBytes = finiteNonNegative(details.lineBytes);
      this.#emit(
        "protocol.anomaly",
        {
          agent: this.processAgent,
          sessionId: null,
          turnId: null,
          spanId: null,
          parentSpanId: null,
        },
        {
          anomaly_kind: safeIdentifier(details.kind, "malformed_json"),
          line_bytes: lineBytes === null ? 0 : Math.floor(lineBytes),
          measurement_quality: "exact",
        },
        monotonicNow,
      );
    });
  }

  async flush(options = {}) {
    try {
      return await this.transport.flush(options);
    } catch {
      this.observerErrors += 1;
      return this.diagnostics();
    }
  }

  async close(options = {}) {
    try {
      await this.transport.close(options);
    } catch {
      this.observerErrors += 1;
    }
  }

  diagnostics() {
    return {
      enabled: this.enabled,
      observerErrors: this.observerErrors,
      sessions: this.sessions.size,
      activeTurns: this.turns.size,
      activeTools: [...this.turns.values()].reduce((count, turn) => count + turn.tools.size, 0),
      transport: this.transport.diagnostics?.() ?? null,
    };
  }
}

function noOpObserver(reason = "disabled") {
  return {
    observeClientMessage() {},
    observeServerMessage() {},
    observeProcessExit() {},
    observeProtocolAnomaly() {},
    async flush() {
      return { enabled: false, reason };
    },
    async close() {},
    diagnostics() {
      return { enabled: false, reason, observerErrors: 0, sessions: 0, activeTurns: 0, activeTools: 0, transport: null };
    },
  };
}

export function createAcpObserver(config = {}) {
  if (config.enabled === false) return noOpObserver();
  try {
    if (!config.identitySalt || String(config.identitySalt).length < 16) {
      return noOpObserver("identity_salt_unavailable");
    }
    if (!config.transport && !config.transportOptions) return noOpObserver("transport_unavailable");
    return new AcpObserver(config);
  } catch {
    return noOpObserver("initialization_failed");
  }
}
