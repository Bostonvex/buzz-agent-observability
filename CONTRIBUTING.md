# Contributing

Changes must preserve the collector's privacy and failure-isolation boundary.

Before submitting a change:

```bash
./scripts/doctor.sh
./scripts/test.sh
```

New event fields require an explicit schema allowlist update, privacy review, documentation, and rejection tests. Generic metadata maps and hidden content-capture modes are not accepted. Mutating routes must use POST, require authentication, and have server-side bounds. Do not add non-loopback binding until an explicit authenticated remote-access design has been reviewed.

Keep harness integrations in separate changes. Protocol-facing instrumentation must forward original ACP output unchanged and remain no-throw, bounded, non-blocking, and fail-open.
