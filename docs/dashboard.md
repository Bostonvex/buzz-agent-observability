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

## Query endpoints

- `GET /api/v1/summary` returns fleet metrics, p50/p95 distributions, grouped
  rollups, and safe filter dimensions.
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
