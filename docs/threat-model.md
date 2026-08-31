# Phase 1 threat model

## Assets and trust boundaries

The collector protects local telemetry integrity, the ingest token, agent identity metadata, and the availability of Buzz and its harnesses. ACP observers are trusted to authenticate but are still treated as untrusted input producers. Browser reads are available only across the workstation loopback boundary.

## Threats and current controls

| Threat | Phase 1 control |
|---|---|
| LAN exposure or drive-by access | Literal `127.0.0.1` bind is enforced in code; non-loopback startup fails. |
| Unauthorized event injection | Random local bearer token stored in a non-symlink regular file with mode `0600`. |
| Oversized bodies or batches | 256 KiB body limit and 100-event batch limit before validation or storage. |
| Malicious or malformed JSON | Strict UTF-8 JSON parsing, exact envelope fields, typed attributes, size limits, and safe error responses. |
| Prompt, response, tool-content, path, or environment leakage | Those fields do not exist in the allowlist; secret-shaped string values are rejected. |
| Persistent browser script injection | Dashboard renders metadata with `textContent`; responses set a restrictive Content Security Policy. |
| SQLite corruption or lock contention | Transactions, busy timeout, WAL mode, and a process-local lock. |
| Unbounded history | Raw events are deleted oldest-first after seven days by periodic retention maintenance. |
| SSRF or command execution | Phase 1 accepts no endpoint URL and contains no model, SSH, or shell execution route. |
| Telemetry blocks ACP forwarding | Not applicable until Phase 2; the proposed observer API requires a bounded queue, short deadlines, and no-throw behavior. |

## Known limits

Loopback is a security boundary, not user authentication for read endpoints: another process running as the same workstation user may read aggregate metadata. TLS and remote access are deliberately unsupported. Database file encryption is not included. The validator detects common secret formats but is not a substitute for preventing sensitive material at the producer.
