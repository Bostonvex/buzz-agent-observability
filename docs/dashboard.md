# Dashboard and query API

The loopback dashboard provides fleet, agent, and turn views over real ACP
telemetry. The default view covers the last 24 hours and can be filtered by
agent, harness, model, endpoint, and outcome. Browser updates arrive over the
sanitized SSE stream; a ten-second refresh is retained as a recovery path.

Latency values always carry a measurement-quality label. Missing data is shown
as `unavailable` and is not silently estimated. Model-server and hardware
samples shown beside a turn are labeled shared timestamp context and are never
attributed to an individual agent without an exact correlation source.
The shared infrastructure panel graphs recent allowlisted series independently
and shows configured/degraded provider counts from the health endpoint.

The tool-observation summary reports terminal-turn coverage and total observed
calls. Recent turns label the column **Observed tools**: `0` means the harness
confirmed that no ACP tool update occurred, while `—` means observation was
unavailable. Hovering the value identifies the observation mode.

The inference-performance section separates three different views of model
service behavior:

- **Fleet output tok/s** is total exact output tokens divided by summed exact
  decode time. It is a duration-weighted per-stream rate, not physical server
  capacity.
- **p50 call tok/s** is the median of individual exact call rates.
- **Server output tok/s** is the wall-clock rate derived from positive deltas in
  the vLLM `generation_tokens_total` counter. Counter decreases are treated as
  resets and never become artificial throughput spikes.

The same section reports exact TTFT and input-token p50/p95 distributions.
Decode concurrency is sampled at each exact call's decode midpoint and grouped
into 1, 2, 3–4, 5–8, and 9+ active-stream bands. Each band shows the weighted
per-stream rate for calls in that band. Concurrency is computed independently
per endpoint within the current model-event filter scope, so the unfiltered
fleet view is authoritative for capacity analysis.

## Query endpoints

- `GET /api/v1/summary` returns fleet metrics, exact model performance
  distributions, decode-concurrency bands, shared infrastructure summaries,
  grouped rollups, and safe filter dimensions. This response can be retained
  as a metadata-only before/after performance report.
- `GET /api/v1/agents/{id}/summary` returns one agent's aggregate and recent
  turns.
- `GET /api/v1/turns/{id}` returns a metadata-only waterfall and separately
  labeled shared context.
- `GET /api/v1/samples` returns at most 500 recent shared model-server and
  hardware samples.
- `GET /api/v1/export.csv` returns at most 500 filtered turn rows and protects
  spreadsheet consumers from formula injection.

List queries accept `since`, `until`, `agent`, `harness`, `model`, `endpoint`,
and `outcome`. Turn lists also accept bounded `limit` and `offset` values.
Explicit time ranges cannot exceed 180 days; queries without a range default
to the last 30 days. All responses retain server-side row caps.
