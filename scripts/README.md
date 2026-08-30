# Management Scripts

Operational utilities configured via `/etc/homelab/homelab.env` (override with `HOMELAB_ENV_FILE`). See [`homelab.env.example`](homelab.env.example) for environment variable definitions.

Scripts require explicit configuration and will fail with missing-variable errors if required environment variables are unset.

---

## `arr_cli.py` — unified media stack CLI

Command-line interface managing Jellyseerr, Radarr, Sonarr, Prowlarr, download clients, and Jellyfin. Used by the Discord bridge, administrative agent, and manual operator shell.

**Core features:**

- `parse_media_query` — Extracts title, year, season, and quality tags from free-text queries.
- `rank_results` — Scores search results by popularity, vote count, exact title match, and release year.
- `estimate_download_size` — Calculates runtime-based size estimates for movies and series prior to queuing.
- `queue auto-remediate` — Detects stalled queue items, removes them, blocklists the failed release, and triggers alternative search.

**Usage:**

```bash
# Aggregate view: active downloads plus in-progress requests
python3 scripts/arr_cli.py status
python3 scripts/arr_cli.py pipeline-details <request_id>

# Discovery and requests
python3 scripts/arr_cli.py jellyseerr search "example series season 2"
python3 scripts/arr_cli.py jellyseerr details --media-type movie --id <id>
python3 scripts/arr_cli.py jellyseerr request --media-type tv --id <id> --seasons "1,2"

# Queue health
python3 scripts/arr_cli.py radarr queue
python3 scripts/arr_cli.py sonarr queue
python3 scripts/arr_cli.py queue auto-remediate

# Indexers: health, and clearing the backoff lockout
python3 scripts/arr_cli.py prowlarr status
python3 scripts/arr_cli.py prowlarr test-all

# Download client and media server
python3 scripts/arr_cli.py downloader list
python3 scripts/arr_cli.py jellyfin refresh
python3 scripts/arr_cli.py jellyfin sessions
```

Append `--json` to any command for machine-readable output.

---

## `discord_bridge.py` — dual-plane Discord gateway

Provides two isolated interfaces:

- **Household plane** — Members interact via private `#req-<username>` channels. Commands include `/request` (catalogue search, size estimation, download confirmation) and `/status` (real-time progress). Responses are ephemeral.
- **Operator plane** — Admins run `!agent` or `/agent` in the ops channel to invoke an SRE coding-agent CLI grounded on this repository. Execution is serialized through a FIFO queue with secret scrubbing applied before Discord transmission.

The agent CLI is configurable via `AGENT_CLI_BIN` and `AGENT_CLI_ARGS` to support any non-interactive agent executable.

Requires `discord.py`. Deployed via [`config/templates/systemd/homelab-bot.service`](../config/templates/systemd/homelab-bot.service). Runbook: [docs/admin_discord_agent.md](../docs/admin_discord_agent.md).

---

## `check_storage.sh` — storage watchdog

Monitors the external media mount point. If the mount disconnects, the script stops dependent containers, cycles the filesystem mount, restarts containers, and triggers a Jellyfin library rescan.

This prevents running download clients from writing into an unmounted mount point on the host root filesystem. A 15-second systemd timer executes the check, using `flock` to prevent concurrent executions.

Create `/tmp/maintenance.lock` to suspend watchdog execution during maintenance.

Deployed via [`config/templates/systemd/storage-watchdog.{service,timer}`](../config/templates/systemd/). Runbook: [docs/admin_storage_watchdog.md](../docs/admin_storage_watchdog.md).
