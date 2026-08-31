const $ = (selector) => document.querySelector(selector);

function cell(value, className = "") {
  const node = document.createElement("td");
  node.textContent = value ?? "—";
  if (className) node.className = className;
  return node;
}

function formatMs(value) {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString();
}

function emptyRow(columns, message) {
  const row = document.createElement("tr");
  const data = cell(message, "empty");
  data.colSpan = columns;
  row.append(data);
  return row;
}

function renderAgents(agents) {
  const body = $("#agents-body");
  body.replaceChildren();
  if (!agents.length) {
    body.append(emptyRow(5, "No agents observed. Load the safe synthetic demo or connect an observer."));
    return;
  }
  for (const agent of agents) {
    const row = document.createElement("tr");
    row.append(cell(agent.display_name, "agent-name"));
    row.append(cell(agent.current_state?.replaceAll("_", " "), `state state-${agent.current_state}`));
    row.append(cell(agent.harness));
    row.append(cell(agent.model));
    row.append(cell(formatTime(agent.last_seen_at)));
    body.append(row);
  }
}

function renderTurns(turns) {
  const body = $("#turns-body");
  body.replaceChildren();
  if (!turns.length) {
    body.append(emptyRow(5, "No turns stored yet."));
    return;
  }
  for (const turn of turns) {
    const row = document.createElement("tr");
    row.append(cell(turn.agent_display_name, "agent-name"));
    row.append(cell(turn.outcome || "active", `state state-${turn.outcome || "active"}`));
    row.append(cell(formatMs(turn.ttfa_ms)));
    row.append(cell(formatMs(turn.ttfvt_ms)));
    row.append(cell(formatMs(turn.duration_ms)));
    body.append(row);
  }
}

async function refresh() {
  try {
    const [healthResponse, agentsResponse, turnsResponse] = await Promise.all([
      fetch("/healthz"),
      fetch("/api/v1/agents?limit=100"),
      fetch("/api/v1/turns?limit=100"),
    ]);
    if (!healthResponse.ok || !agentsResponse.ok || !turnsResponse.ok) throw new Error("collector query failed");
    const [health, agents, turns] = await Promise.all([
      healthResponse.json(),
      agentsResponse.json(),
      turnsResponse.json(),
    ]);
    $("#health").className = "health healthy";
    $("#health span:last-child").textContent = "Collector healthy";
    $("#event-count").textContent = health.events;
    $("#agent-count").textContent = health.agents;
    $("#turn-count").textContent = health.turns;
    $("#journal-mode").textContent = health.journal_mode.toUpperCase();
    $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderAgents(agents.agents);
    renderTurns(turns.turns);
  } catch {
    $("#health").className = "health unhealthy";
    $("#health span:last-child").textContent = "Collector disconnected";
  }
}

function connectLive() {
  const stream = new EventSource("/api/v1/live");
  stream.addEventListener("telemetry", refresh);
  stream.onerror = () => {
    $("#health").className = "health unhealthy";
    $("#health span:last-child").textContent = "Reconnecting";
  };
}

refresh();
connectLive();
setInterval(refresh, 5000);
