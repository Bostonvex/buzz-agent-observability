# ZCode integration

ZCode telemetry is integrated in the public `buzz-zcode-harness` repository at
native ACP handler boundaries. The bridge observes lifecycle requests and the
live `session/update` delivery helper, rather than inserting a byte-stream
proxy between the SDK and stdio.

Configuration uses the same shared variables documented in the
[DeepSeek integration](deepseek.md). The integration is disabled by default.
Missing or unsafe private files, invalid configuration, observer exceptions,
timeouts, and collector outages all fail open.

For exact Anthropic Messages TTFT, token counts, decode time, and output tokens
per second, install the optional proxy and add:

```text
BUZZ_MODEL_PROXY_ENABLED=1
BUZZ_MODEL_PROXY_BIN=/absolute/path/to/buzz-model-proxy
```

The bridge resolves its configured Anthropic base URL only when supervised
proxy mode is requested, starts an ephemeral loopback sidecar, and redirects
only the ZCode model subprocess. The proxy receives neither
`ANTHROPIC_API_KEY` nor other model credentials; the model child receives
neither collector token paths nor proxy controls. Missing configuration or a
startup failure falls back to the direct upstream. An unexpected proxy exit
terminates the harness so Buzz can restart the supervised process tree.

Before observation, the ZCode adapter reduces each message to the minimum
shape needed by the shared observer. It removes prompt and completion text,
reasoning, tool titles/arguments/results, paths, arbitrary metadata, response
extensions, and unapproved environment entries. The shared observer hashes
raw session and tool-call identifiers before any event leaves the process.

History replay intentionally bypasses the live update hook, so reconnecting
does not appear as fresh activity. ZCode's out-of-band background task
broadcast path also bypasses the hook, preventing a task that finishes later
from being charged to an unrelated foreground turn. The integration suite
proves both exclusions, exactly-once live delivery, content reduction, and
fail-open behavior.
