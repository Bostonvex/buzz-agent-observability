const $ = (selector) => document.querySelector(selector);
const state = { health: null, agents: [], turns: [], summary: null, refreshTimer: null };

function node(tag, text, className = "") {
  const element = document.createElement(tag);
  if (text !== null && text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function cell(value, className = "") {
  return node("td", value ?? "—", className);
}

function formatMs(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 60_000) return `${(value / 60_000).toFixed(1)} min`;
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatRate(value) {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

function metricQuality(metric) {
  if (!metric?.count) return "unavailable";
  const qualities = Object.keys(metric.quality_counts || {}).filter((name) => metric.quality_counts[name]);
  return qualities.length === 1 ? qualities[0] : "derived";
}

function emptyRow(columns, message) {
  const row = document.createElement("tr");
  const data = cell(message, "empty");
  data.colSpan = columns;
  row.append(data);
  return row;
}

function quality(value, unavailable = false) {
  const label = unavailable ? "unavailable" : value || "exact";
  return node("span", label, `quality ${label}`);
}

function queryParameters() {
  const parameters = new URLSearchParams();
  const hours = Number($("#filter-time").value);
  parameters.set("since", new Date(Date.now() - hours * 60 * 60 * 1000).toISOString());
  for (const [id, key] of [
    ["#filter-agent", "agent"], ["#filter-harness", "harness"], ["#filter-model", "model"],
    ["#filter-endpoint", "endpoint"], ["#filter-outcome", "outcome"],
  ]) {
    const value = $(id).value;
    if (value) parameters.set(key, value);
  }
  return parameters;
}

async function fetchJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`query failed: ${response.status}`);
  return response.json();
}

function fillSelect(selector, values, labelForValue = (value) => value) {
  const select = $(selector);
  const selected = select.value;
  const first = select.options[0];
  select.replaceChildren(first);
  for (const value of values) {
    const option = node("option", labelForValue(value));
    option.value = value;
    select.append(option);
  }
  if (values.includes(selected)) select.value = selected;
}

function renderFilters() {
  const dimensions = state.summary?.dimensions;
  if (!dimensions) return;
  const names = new Map(state.agents.map((agent) => [agent.id, agent.display_name]));
  fillSelect("#filter-agent", dimensions.agents, (value) => names.get(value) || `Unknown · ${value.slice(0, 8)}`);
  fillSelect("#filter-harness", dimensions.harnesses);
  fillSelect("#filter-model", dimensions.models);
  fillSelect("#filter-endpoint", dimensions.endpoints);
}

function renderSummary() {
  const fleet = state.summary.fleet;
  $("#active-agent-count").textContent = fleet.active_agents;
  $("#turn-count").textContent = fleet.turn_count;
  $("#duration-p50").textContent = formatMs(fleet.metrics.duration_ms.p50);
  $("#duration-p95").textContent = formatMs(fleet.metrics.duration_ms.p95);
  const durationQuality = metricQuality(fleet.metrics.duration_ms);
  for (const badge of [$("#duration-p50-quality"), $("#duration-p95-quality")]) {
    badge.textContent = durationQuality;
    badge.className = `quality ${durationQuality}`;
  }
  $("#success-rate").textContent = formatRate(fleet.success_rate);
  $("#event-count").textContent = state.health.events;
  $("#journal-mode").textContent = `SQLite ${state.health.journal_mode.toUpperCase()}`;
  $("#turn-outcomes").textContent = `${fleet.outcomes.failed || 0} failed · ${fleet.outcomes.cancelled || 0} cancelled`;
  const coverage = fleet.metrics.ttfvt_ms.count;
  const banner = $("#status-banner");
  if (fleet.turn_count > 0 && coverage < fleet.turn_count) {
    banner.hidden = false;
    banner.className = "status-banner partial";
    banner.textContent = `Partial data: visible-text timing is available for ${coverage} of ${fleet.turn_count} turns. Missing metrics remain unavailable; they are not estimated.`;
  } else {
    banner.hidden = true;
  }
}

function renderAgents() {
  const body = $("#agents-body");
  body.replaceChildren();
  if (!state.agents.length) {
    body.append(emptyRow(6, "No agents observed in this window. Try a wider time range or connect an instrumented harness."));
    return;
  }
  for (const agent of state.agents) {
    const row = document.createElement("tr");
    const agentButton = node("button", agent.display_name || "Unknown agent", "row-link");
    agentButton.type = "button";
    agentButton.dataset.agentId = agent.id;
    const agentCell = document.createElement("td");
    agentCell.append(agentButton);
    if ((agent.display_name || "").startsWith("Unknown agent")) agentCell.append(quality("unavailable", true));
    row.append(agentCell);
    row.append(cell((agent.current_state || "unknown").replaceAll("_", " "), `state state-${agent.current_state || "unknown"}`));
    row.append(cell(`${agent.harness || "Unknown harness"} / ${agent.model || "Unknown model"}`, "dimension"));
    row.append(cell(agent.endpoint_id || "Unassigned", "muted"));
    const active = agent.current_turn_id && agent.current_turn_started_at
      ? formatMs(Math.max(0, Date.now() - new Date(agent.current_turn_started_at).getTime()))
      : "—";
    row.append(cell(active, agent.current_turn_id ? "live-elapsed" : "muted"));
    row.append(cell(formatTime(agent.last_seen_at), "muted"));
    body.append(row);
  }
}

function renderTurns() {
  const body = $("#turns-body");
  body.replaceChildren();
  if (!state.turns.length) {
    body.append(emptyRow(8, "No turns stored in this window."));
    return;
  }
  for (const turn of state.turns) {
    const row = document.createElement("tr");
    const button = node("button", turn.agent_display_name || "Unknown agent", "row-link");
    button.type = "button";
    button.dataset.turnId = turn.id;
    const agentCell = document.createElement("td");
    agentCell.append(button);
    row.append(agentCell);
    const outcome = turn.outcome || "active";
    row.append(cell(outcome, `state state-${outcome}`));
    row.append(cell(formatMs(turn.ttfa_ms)));
    row.append(cell(formatMs(turn.ttfvt_ms)));
    row.append(cell(formatMs(turn.first_tool_ms)));
    row.append(cell(formatMs(turn.duration_ms)));
    row.append(cell(turn.tool_count ?? "—"));
    const qualityCell = document.createElement("td");
    qualityCell.append(quality(turn.measurement_quality, turn.duration_ms === null));
    row.append(qualityCell);
    body.append(row);
  }
}

function metricCard(label, value, badge = null) {
  const card = node("article", null, "detail-card");
  card.append(node("span", label), node("strong", value));
  if (badge) card.append(quality(badge, badge === "unavailable"));
  return card;
}

function breakdownRow(label, value, maximum) {
  const row = node("div", null, "breakdown-row");
  row.append(node("span", label), node("strong", String(value)));
  const track = node("div", null, "mini-track");
  const fill = node("i");
  fill.style.width = `${maximum ? (value / maximum) * 100 : 0}%`;
  track.append(fill);
  row.append(track);
  return row;
}

async function openAgent(agentId) {
  const parameters = queryParameters();
  parameters.delete("agent");
  const detail = await fetchJson(`/api/v1/agents/${encodeURIComponent(agentId)}/summary?${parameters}`);
  const { agent, aggregate } = detail;
  $("#agent-title").textContent = agent.display_name || "Unknown agent";
  $("#agent-subtitle").textContent = `${agent.harness || "Unknown harness"} · ${agent.model || "Unknown model"} · ${agent.endpoint_id || "No endpoint"}`;
  const cards = $("#agent-metrics");
  cards.replaceChildren(
    metricCard("Turns", aggregate.turn_count),
    metricCard("p50 TTFA", formatMs(aggregate.metrics.ttfa_ms.p50), metricQuality(aggregate.metrics.ttfa_ms)),
    metricCard("p95 visible text", formatMs(aggregate.metrics.ttfvt_ms.p95), metricQuality(aggregate.metrics.ttfvt_ms)),
    metricCard("p95 duration", formatMs(aggregate.metrics.duration_ms.p95), metricQuality(aggregate.metrics.duration_ms)),
    metricCard("Longest stall", formatMs(aggregate.metrics.max_stall_ms.maximum), metricQuality(aggregate.metrics.max_stall_ms)),
  );
  const outcomes = $("#agent-outcomes");
  outcomes.replaceChildren();
  const maximum = Math.max(1, aggregate.turn_count);
  for (const name of ["completed", "failed", "cancelled", "active"]) outcomes.append(breakdownRow(name, aggregate.outcomes[name] || 0, maximum));
  const coverage = $("#agent-coverage");
  coverage.replaceChildren();
  for (const [label, key] of [["First activity", "ttfa_ms"], ["Visible text", "ttfvt_ms"], ["First tool", "first_tool_ms"], ["Total duration", "duration_ms"]]) {
    coverage.append(breakdownRow(label, aggregate.metrics[key].count, maximum));
  }
  $("#turn-view").hidden = true;
  $("#agent-view").hidden = false;
  $("#agent-view").scrollIntoView({ behavior: "smooth", block: "start" });
  history.replaceState(null, "", `#agent=${encodeURIComponent(agentId)}`);
}

function eventLabel(event) {
  const labels = {
    "turn.started": "Prompt received", "turn.first_activity": "First activity", "turn.first_visible_text": "First visible text",
    "turn.first_tool": "First tool", "turn.stall": "Stall detected", "turn.completed": "Turn completed",
    "turn.failed": "Turn failed", "turn.cancelled": "Turn cancelled", "tool.started": "Tool started",
    "tool.updated": "Tool update", "tool.completed": "Tool completed", "tool.failed": "Tool failed",
    "usage.updated": "Usage sample", "model.request_started": "Model request", "model.first_token": "Model first token",
    "model.completed": "Model completed", "model.failed": "Model failed", "server.sample": "Model server sample", "hardware.sample": "Hardware sample",
  };
  return labels[event.event_type] || event.event_type.replaceAll(".", " ");
}

function renderWaterfall(events, durationMs) {
  const container = $("#waterfall");
  container.replaceChildren();
  if (!events.length) {
    container.append(node("p", "Timeline detail expired or is unavailable.", "empty-card"));
    return;
  }
  const origin = events.find((event) => event.event_type === "turn.started")?.monotonic_offset_ms ?? events[0].monotonic_offset_ms;
  const eventOffset = (event) => Number.isFinite(event.relative_ms) ? event.relative_ms : event.monotonic_offset_ms - origin;
  const observedMaximum = Math.max(...events.map(eventOffset), 1);
  const total = Math.max(durationMs || 0, observedMaximum, 1);
  for (const event of events) {
    const offset = Math.max(0, eventOffset(event));
    const duration = Number(event.attributes.duration_ms || 0);
    const start = Math.max(0, offset - duration);
    const row = node("div", null, "waterfall-row");
    const label = node("div", null, "waterfall-label");
    label.append(node("strong", eventLabel(event)), node("span", formatMs(offset)));
    const track = node("div", null, "waterfall-track");
    const bar = node("i", null, `waterfall-bar quality-${event.attributes.measurement_quality || "exact"}`);
    bar.style.left = `${(start / total) * 100}%`;
    bar.style.width = `${Math.max(0.7, (Math.max(duration, 1) / total) * 100)}%`;
    track.append(bar);
    row.append(label, track);
    container.append(row);
  }
}

async function openTurn(turnId) {
  const detail = await fetchJson(`/api/v1/turns/${encodeURIComponent(turnId)}`);
  const turn = detail.turn;
  const model = detail.model_metrics || {};
  $("#turn-title").textContent = `${turn.agent_display_name || "Unknown agent"} · turn waterfall`;
  $("#turn-subtitle").textContent = `${turn.harness || "Unknown harness"} / ${turn.model || "Unknown model"} · ${formatTime(turn.started_at)} · ${turn.outcome || "active"}`;
  $("#turn-facts").replaceChildren(
    metricCard("First activity", formatMs(turn.ttfa_ms), turn.ttfa_ms === null ? "unavailable" : turn.measurement_quality || "exact"),
    metricCard("Visible text", formatMs(turn.ttfvt_ms), turn.ttfvt_ms === null ? "unavailable" : turn.measurement_quality || "exact"),
    metricCard("First tool", formatMs(turn.first_tool_ms), turn.first_tool_ms === null ? "unavailable" : turn.measurement_quality || "exact"),
    metricCard("Total", formatMs(turn.duration_ms), turn.duration_ms === null ? "unavailable" : turn.measurement_quality || "exact"),
    metricCard("Longest stall", formatMs(turn.max_stall_ms), turn.max_stall_ms === null ? "unavailable" : turn.measurement_quality || "exact"),
    metricCard("Model calls", model.call_count ?? 0, model.call_count ? "exact" : "unavailable"),
    metricCard("Model p50 TTFT", formatMs(model.ttft_ms?.p50), model.ttft_ms?.count ? "exact" : "unavailable"),
    metricCard("Model output tok/s", model.output_tokens_per_second === null || model.output_tokens_per_second === undefined ? "—" : model.output_tokens_per_second.toFixed(1), model.output_tokens_per_second === null || model.output_tokens_per_second === undefined ? "unavailable" : "exact"),
  );
  renderWaterfall(detail.timeline, turn.duration_ms);
  const shared = $("#shared-context");
  const sharedEvents = $("#shared-events");
  sharedEvents.replaceChildren();
  shared.hidden = detail.shared_context.length === 0;
  for (const event of detail.shared_context) {
    sharedEvents.append(node("span", `${formatTime(event.observed_at)} · ${eventLabel(event)}`, "shared-chip"));
  }
  $("#agent-view").hidden = true;
  $("#turn-view").hidden = false;
  $("#turn-view").scrollIntoView({ behavior: "smooth", block: "start" });
  history.replaceState(null, "", `#turn=${encodeURIComponent(turnId)}`);
}

function setConnection(label, className) {
  $("#health").className = `health ${className}`;
  $("#health span:last-child").textContent = label;
}

async function refresh() {
  const parameters = queryParameters();
  const suffix = parameters.toString();
  try {
    const [health, agents, turns, summary] = await Promise.all([
      fetchJson("/healthz"), fetchJson(`/api/v1/agents?limit=100&${suffix}`),
      fetchJson(`/api/v1/turns?limit=100&${suffix}`), fetchJson(`/api/v1/summary?${suffix}`),
    ]);
    Object.assign(state, { health, agents: agents.agents, turns: turns.turns, summary });
    setConnection("Collector live", "healthy");
    $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    $("#export-link").href = `/api/v1/export.csv?${suffix}`;
    renderSummary();
    renderAgents();
    renderTurns();
    renderFilters();
  } catch {
    setConnection("Collector disconnected", "unhealthy");
    const banner = $("#status-banner");
    banner.hidden = false;
    banner.className = "status-banner disconnected";
    banner.textContent = "Collector unavailable. The dashboard will reconnect automatically; agent execution is unaffected.";
  }
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(refresh, 120);
}

function connectLive() {
  const stream = new EventSource("/api/v1/live");
  stream.addEventListener("ready", () => setConnection("Collector live", "healthy"));
  stream.addEventListener("telemetry", scheduleRefresh);
  stream.onerror = () => setConnection("Reconnecting", "unhealthy");
}

document.addEventListener("click", (event) => {
  const agentButton = event.target.closest("[data-agent-id]");
  const turnButton = event.target.closest("[data-turn-id]");
  if (agentButton) void openAgent(agentButton.dataset.agentId);
  if (turnButton) void openTurn(turnButton.dataset.turnId);
});

for (const select of document.querySelectorAll(".filters select")) select.addEventListener("change", refresh);
$("#clear-filters").addEventListener("click", () => {
  for (const select of document.querySelectorAll(".filters select")) select.selectedIndex = select.id === "filter-time" ? 1 : 0;
  void refresh();
});
for (const button of document.querySelectorAll(".close-detail")) button.addEventListener("click", () => {
  $("#agent-view").hidden = true;
  $("#turn-view").hidden = true;
  history.replaceState(null, "", location.pathname);
});

void refresh().then(() => {
  const match = location.hash.match(/^#(agent|turn)=(.+)$/);
  if (match?.[1] === "agent") void openAgent(decodeURIComponent(match[2]));
  if (match?.[1] === "turn") void openTurn(decodeURIComponent(match[2]));
});
connectLive();
setInterval(refresh, 10_000);
setInterval(renderAgents, 1_000);
