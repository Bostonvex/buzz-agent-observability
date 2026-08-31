# Privacy boundary

Buzz Agent Observability stores operational metadata only: identities chosen through approved non-secret metadata, harness and model labels, hashed session identifiers, event timestamps, timing measurements, counts, outcomes, and measurement-quality labels.

It does not collect or persist:

- prompts, responses, reasoning text, or model stream content;
- tool arguments, tool results, file contents, or workspace paths;
- headers, cookies, API keys, tokens, private keys, authentication tags, or environment dumps;
- arbitrary producer-defined fields or attributes.

The collector validates before writing to SQLite. An event containing an unknown field, unknown attribute, control characters, or a common secret-shaped string is rejected as a whole. API validation errors identify only the field path and error category; they never echo the submitted value.

Friendly agent names remain separate from stable privacy-preserving IDs. Future ACP observers must never use owner authorization metadata or private key material for identity. If a safe identity cannot be resolved, the observer should emit an unknown/session-derived identity and expose the mapping issue diagnostically.
