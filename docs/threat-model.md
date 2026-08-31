# Threat model

## Assets and trust boundaries

The collector protects local telemetry integrity, the ingest token, agent identity metadata, and the availability of Buzz and its harnesses. ACP observers are trusted to authenticate but are still treated as untrusted input producers. Browser reads are available only across the workstation loopback boundary.

## Threats and current controls

| Threat | Current control |
|---|---|
| LAN exposure or drive-by access | Literal `127.0.0.1` bind is enforced in code; non-loopback startup fails. |
| Unauthorized event injection | Random local bearer token stored in a non-symlink regular file with mode `0600`. |
| Oversized bodies or batches | 256 KiB body limit and 100-event batch limit before validation or storage. |
| Malicious or malformed JSON | Strict UTF-8 JSON parsing, exact envelope fields, typed attributes, size limits, and safe error responses. |
| Prompt, response, tool-content, path, or environment leakage | Those fields do not exist in the allowlist; secret-shaped string values are rejected. |
| Persistent browser script injection | Dashboard renders metadata with `textContent`; responses set a restrictive Content Security Policy. |
| SQLite corruption or lock contention | Transactions, busy timeout, WAL mode, and a process-local lock. |
| Unbounded history | Raw events are deleted oldest-first after seven days by periodic retention maintenance. |
| Model-proxy SSRF or credential disclosure | The optional proxy accepts one startup-configured upstream, fixes its allowed request paths, strips telemetry headers, and never logs/stores model headers or bodies. |
| Forged model correlation | The proxy context endpoint requires the private collector token and accepts only the normalized identifier shape. Concurrent active turns are marked ambiguous unless explicitly selected. |
| Telemetry blocks ACP or model forwarding | Observer and proxy delivery use bounded queues, short deadlines, no-throw entry points, and fail-open collector handling. |
| Provider SSRF, redirects, or oversized metrics | vLLM uses one startup-configured `/metrics` URL without credentials/query/fragment, rejects redirects, allowlists metric families, discards labels, and caps the response. |
| Command injection or mutation through hardware polling | Local/remote NVIDIA commands are fixed argv arrays with no shell; remote destinations reject metacharacters and require batch mode plus normal SSH host verification. |
| Arbitrary generic-provider data | The executable must be absolute, argv is fixed at startup, stdout and time are bounded, stderr is discarded, and exact JSON fields/metric allowlists are enforced. |
| Provider failure affects collection | Every provider has an independent thread and poll boundary; safe status counters are exposed while ingestion and agent execution continue. |
| Release leaks local data or secrets | Source and archive scanners reject common credentials and workstation home paths; wheel contents, archive paths, sizes, and version metadata are checked before release. |

## Known limits

Loopback is a security boundary, not user authentication for read endpoints: another process running as the same workstation user may read aggregate metadata. TLS and remote access are deliberately unsupported. Database file encryption is not included. The validator detects common secret formats but is not a substitute for preventing sensitive material at the producer. A configured generic executable and the user's SSH client remain inside the operator trust boundary; the collector constrains invocation and persistence but cannot prove that an external program is read-only.
