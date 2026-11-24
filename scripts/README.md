# Management Scripts

Operational utilities configured via `/etc/homelab/homelab.env` (override with `HOMELAB_ENV_FILE`). See [`homelab.env.example`](homelab.env.example) for environment variable definitions.

Scripts require explicit configuration and will fail with missing-variable errors if required environment variables are unset.

---

## `arr_cli.py` — unified media stack CLI

Command-line interface managing Jellyseerr, Radarr, Sonarr, Prowlarr, download clients, Bazarr, and Jellyfin. Used by the Discord bridge, administrative agent, and manual operator shell.

**Core features:**

- `parse_media_query` — Extracts title, year, season, and quality tags from free-text queries.
- `rank_results` — Scores search results by popularity, vote count, exact title match, and release year.
- `estimate_download_size` — Calculates runtime-based size estimates for movies and series prior to queuing.
- `pipeline-details` — Provides comprehensive 5-stage diagnostics (`[Portal] ➔ [Arr Stack] ➔ [Download Client] ➔ [Import] ➔ [Jellyfin]`).
- `pipeline-cancel` — Safely cancels active in-progress requests and cleans queues before completion.
- `switch-quality` — Modifies quality profile targets on active items and triggers alternative release searches.
- `history` — Queries comprehensive media request history with timestamps and status tracking.
- `queue auto-remediate` — Detects stalled items, cleans queues, blocklists failed releases, and triggers alternative searches.
- `subtitles` & `bazarr` — Coverage checking, synchronization, and subtitle daemon management.
- `discord-role` — Administrator role assignment and revocation with Owner immutability enforcement.

**Usage:**

```bash
# Aggregate view: active downloads plus in-progress requests
python3 scripts/arr_cli.py status [--user <user>] [--take 10]
python3 scripts/arr_cli.py pipeline-details <request_id|arr_id>
python3 scripts/arr_cli.py pipeline-cancel <request_id|title>
python3 scripts/arr_cli.py switch-quality <id|title> --quality <1080p|720p|4k>
python3 scripts/arr_cli.py history [--user <user>] [--take 50]

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

# Subtitles & Bazarr
python3 scripts/arr_cli.py subtitles status
python3 scripts/arr_cli.py subtitles sync [--dry-run]
python3 scripts/arr_cli.py bazarr status
python3 scripts/arr_cli.py bazarr search-missing
python3 scripts/arr_cli.py bazarr sync

# Discord role management (Admins only, Owner immutable)
python3 scripts/arr_cli.py discord-role list <user>
python3 scripts/arr_cli.py discord-role grant <user> <role>
python3 scripts/arr_cli.py discord-role revoke <user> <role>
```

Append `--json` to any command for machine-readable output.

---

## `discord_bridge.py` — dual-plane Discord gateway

Provides two isolated interfaces:

- **Household plane** — Members interact via private `#req-<username>` channels or DMs. Commands include `/request` (catalogue search, size estimation, storage pre-flight gate, download confirmation), `/status` (real-time progress, request timestamps, details view), `/cancel` (user-scoped cancellation of active downloads), `/history` (request log), and `/open-channel`.
- **Operator plane** — Administrators can run `!agent` or `/agent` from the configured ops channel or its threads. Execution is serialized with secret scrubbing and typing heartbeats. Admins can also manage roles via `/role grant/revoke/list`.

The agent CLI is configurable via `AGENT_CLI_BIN` and `AGENT_CLI_ARGS` to support any non-interactive agent executable.

Requires `discord.py`. Deployed via [`config/templates/systemd/homelab-bot.service`](../config/templates/systemd/homelab-bot.service). Runbook: [docs/admin_discord_agent.md](../docs/admin_discord_agent.md).

---

## `sync_subtitles.py` — subtitle synchronization engine

Bulk subtitle remediation engine that ensures 100% English subtitle coverage across movies and TV series:
- Scans Jellyfin library items for missing English audio or subtitle streams.
- Downloads standardized external `.en.srt` files from Gestdown, YIFY, and SubDL.
- Sets appropriate container file permissions and triggers Jellyfin/Bazarr library refreshes.

Run directly via `python3 scripts/sync_subtitles.py [--dry-run]` or via `python3 scripts/arr_cli.py subtitles sync`.

---

## `check_storage.sh` — storage watchdog

Uses `media_storage.py` to verify the expected ext4 filesystem and durable access. The watchdog stops dependent containers if verification fails and starts only previously running containers after verified recovery. The `media-storage-hook.sh` pre-start hook independently gates container startup.

This prevents running download clients from writing into an unmounted mount point on the host root filesystem. A 15-second systemd timer executes the check.

Create `/tmp/maintenance.lock` to suspend watchdog execution during maintenance.

Deployed via [`config/templates/systemd/storage-watchdog.{service,timer}`](../config/templates/systemd/). Runbook: [docs/admin_storage_watchdog.md](../docs/admin_storage_watchdog.md).
