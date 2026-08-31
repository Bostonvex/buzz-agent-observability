# Upstream source and license review

## Decision

The upstream project is [tonyd2wild/2Wild-Coding-Agent-Latency-Monitor](https://github.com/tonyd2wild/2Wild-Coding-Agent-Latency-Monitor), reviewed at commit `4a3080f1260cb40d1cc05fd308aae1f61c4a5853` dated 2026-07-23. It is licensed under the MIT License.

MIT permits use, modification, distribution, and sublicensing when its copyright and permission notice accompany copied or substantial portions. Phase 1 copies no upstream source, so it does not require an embedded upstream notice. The pinned source and license remain documented in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for provenance and future reuse decisions.

## What is reused

Concepts only:

- A lightweight local server paired with static browser assets.
- A dense health-oriented dashboard presentation.
- Timestamp-correlated model-server metrics as fleet context.
- Graceful handling of unavailable model and hardware telemetry.

The Phase 1 HTTP server, database, validator, dashboard markup, styles, and tests were written from scratch for this project.

## Endpoint and control inventory

The reviewed server exposes static content and reads through GET endpoints such as `/presets`, `/harness`, `/runs`, `/hw`, and `/api/fleet`. It also starts or stops work through GET endpoints including `/runall`, `/artall`, `/agentrun`, and `/kill`, and appends browser-provided records through unauthenticated `POST /save`.

Browser-controlled values include the upstream endpoint and model, prompts and history, maximum tokens, temperature, thinking mode, concurrency, harness profile, artificial tool delay, number of turns, and art dimensions. Model probes and generation requests use the submitted endpoint. Concurrency is bounded in some routes but permits up to 256 streams; body length and stored record shape are not bounded or allowlisted at `/save`.

The server also loads local endpoint, fleet, node, key, and output-directory configuration; writes run history; launches fixed SSH commands for GPU and switch sampling; and can close live model connections. It binds to `0.0.0.0` and does not authenticate its control or write routes.

## Security differences required here

The reviewed source is useful as research, but its operational assumptions do not meet this project's privacy and failure-isolation boundary:

- Phase 1 binds only to literal `127.0.0.1`.
- Ingestion is a bounded authenticated POST; GET requests cannot mutate state.
- Event objects and attributes use strict allowlists before persistence.
- The browser cannot select or submit an upstream URL.
- There is no model execution, process execution, SSH, or kill control in Phase 1.
- If hardware polling is added later, SSH host-key checking must remain enabled and commands must come from a fixed read-only allowlist. The reviewed source explicitly disables strict host-key checking and therefore will not be copied.
- Raw prompts, model output, and browser-provided run records are outside the storage contract.

## Future reuse gate

Any future source reuse must identify the exact files and lines, preserve the upstream MIT notice, receive a focused security review, and include tests for loopback binding, SSRF resistance, bounded input, authentication, and content rejection before merging.
