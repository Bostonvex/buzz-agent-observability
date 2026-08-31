# ZCode integration

ZCode telemetry is integrated in the public `buzz-zcode-harness` repository at
native ACP handler boundaries. The bridge observes lifecycle requests and the
live `session/update` delivery helper, rather than inserting a byte-stream
proxy between the SDK and stdio.

Configuration uses the same shared variables documented in the
[DeepSeek integration](deepseek.md). The integration is disabled by default.
Missing or unsafe private files, invalid configuration, observer exceptions,
timeouts, and collector outages all fail open.

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
