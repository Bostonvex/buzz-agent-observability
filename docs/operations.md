# Workstation operations

These procedures install and operate the loopback service on the Buzz workstation. ACP-only telemetry requires no software, configuration, or credentials on model-serving nodes.

## Build and verify

From a scrubbed release checkout:

```bash
./scripts/doctor.sh
./scripts/test.sh
uv build
python3 scripts/check-release.py --write-checksums
```

The release check rejects unsafe archive paths, oversized archives, common credential patterns, workstation home paths, tests in the wheel, missing runtime files, and incorrect version metadata. Verify `dist/SHA256SUMS` before transferring an artifact.

## Install

```bash
./scripts/install.sh --artifact dist/buzz_agent_observability-0.1.0-py3-none-any.whl
buzz-observability doctor
```

The default layout is:

- Versioned releases: `~/.local/share/buzz-agent-observability/install/releases/`
- Active/previous release symlinks: `~/.local/share/buzz-agent-observability/install/`
- Command symlinks: `~/.local/bin/`
- Configuration and secrets: `~/.config/buzz-agent-observability/`
- Database: `~/.local/share/buzz-agent-observability/telemetry.sqlite3`

Use `--prefix` and `--bin-dir` to override installer paths. Installation never enables or starts a service.

## Diagnose

The default doctor uses disposable files to validate schema, secure token/salt creation, and SQLite WAL behavior:

```bash
buzz-observability doctor
```

To diagnose the real data/config paths:

```bash
buzz-observability doctor \
  --database "$HOME/.local/share/buzz-agent-observability/telemetry.sqlite3" \
  --token-file "$HOME/.config/buzz-agent-observability/ingest-token" \
  --identity-salt-file "$HOME/.config/buzz-agent-observability/identity-salt"
```

Pass provider options to test one provider poll without persistence. See [providers](providers.md).

## Run and service examples

Run interactively first:

```bash
buzz-observability serve
```

The repository includes [launchd](../packaging/launchd/com.buzz.agent-observability.plist.example) and [systemd user-service](../packaging/systemd/buzz-agent-observability.service.example) examples. Copy a template, replace placeholders, validate it, and load/enable it manually only after the interactive doctor and serve checks succeed. No project script loads, enables, disables, or modifies operating-system service state.

Provider flags may be appended to `ProgramArguments` or `ExecStart`. Ensure configuration/data directories exist before using the hardened systemd example.

## Backup

The backup command uses SQLite's online backup API and refuses to overwrite an existing file:

```bash
buzz-observability backup \
  --output "$HOME/.local/share/buzz-agent-observability/backups/before-upgrade.sqlite3"
```

The result is mode `0600`. Copy the ingest token and identity salt separately with equivalent permissions if disaster recovery must preserve agent identity continuity. Never publish any of these files.

## Upgrade

1. Build/verify the new wheel and back up the database.
2. Stop the manually configured service.
3. Install a new versioned release and switch the `current` symlink:

```bash
./scripts/upgrade.sh --artifact dist/buzz_agent_observability-0.1.0-py3-none-any.whl
buzz-observability doctor \
  --database "$HOME/.local/share/buzz-agent-observability/telemetry.sqlite3" \
  --token-file "$HOME/.config/buzz-agent-observability/ingest-token" \
  --identity-salt-file "$HOME/.config/buzz-agent-observability/identity-salt"
```

4. Restart the service manually and check `/healthz`.

## Roll back

Stop the service, switch to the recorded previous version, diagnose, and restart:

```bash
./scripts/rollback.sh
buzz-observability doctor \
  --database "$HOME/.local/share/buzz-agent-observability/telemetry.sqlite3" \
  --token-file "$HOME/.config/buzz-agent-observability/ingest-token" \
  --identity-salt-file "$HOME/.config/buzz-agent-observability/identity-salt"
```

Release 0.1.0 uses schema version 1. For a future release with an incompatible storage migration, restore the pre-upgrade backup before starting the older binary.

## Retention and explicit purge

Raw events expire automatically after seven days by default while normalized turn summaries remain. An operator can delete older raw events explicitly:

```bash
buzz-observability purge \
  --before 2026-08-01T00:00:00Z \
  --confirm-delete-raw-events
```

The confirmation flag is mandatory. This does not remove aggregate turn summaries.

## Recoverable uninstall

First unload/disable any service definition you installed manually. Then:

```bash
./scripts/uninstall.sh
```

The script removes command symlinks only when they point into its managed prefix, moves the versioned installation to a timestamped `.uninstalled` sibling, and preserves configuration, identity material, telemetry, and service files. Delete those retained items only after reviewing and backing up what you need.
