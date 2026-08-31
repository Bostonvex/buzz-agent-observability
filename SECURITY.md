# Security policy

Please do not open a public issue for a suspected vulnerability. Use GitHub's private vulnerability reporting for this repository and include the affected version, reproduction steps, impact, and any suggested mitigation.

Version 0.1.x receives security fixes. The service supports loopback-only operation. A non-loopback bind, reverse proxy, shared-host deployment, or exposure through a tunnel is unsupported and should be treated as unsafe.

Never include real prompts, responses, tool payloads, filesystem paths, hostnames, addresses, credentials, tokens, private keys, or environment dumps in a report. Replace them with synthetic placeholders.

Optional providers must remain read-only, bounded, allowlisted, and disabled by default. Provider or model-proxy credentials must never be placed in repository configuration, command arguments, events, URLs, or issue reports.
