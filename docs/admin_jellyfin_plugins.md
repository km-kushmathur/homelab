# Installing Jellyfin Plugins

Procedure for installing Jellyfin plugins and safely restarting container 201.

> **Pre-flight check:** Verify storage mount status before restarting the container. If the container restarts while the host mount is unmounted, Jellyfin will scan an empty directory and mark existing library items as missing.

## Installation Sequence

**1. Confirm host storage mount:**

```bash
mountpoint -q /mnt/media && echo mounted || echo "NOT MOUNTED"
```

Verify output reports `mounted` before proceeding.

**2. Add plugin repository** in Jellyfin web UI (`http://192.168.1.11:8096`):

1. Navigate to **Dashboard → Plugins → Repositories → +**.
2. Input the official plugin repository manifest URL (`manifest.json`).
3. Save.

**3. Install plugin** from **Dashboard → Plugins → Catalog**. Review required dependencies and configuration notes.

**4. Restart container cleanly:**

```bash
mountpoint -q /mnt/media && echo mounted || echo "NOT MOUNTED"
pct stop 201
pct start 201
```

Use `pct stop` to permit Jellyfin to finalize SQLite database writes on shutdown. Avoid forced termination.

## Operational Guidelines

- Add repositories only from verified project manifest URLs.
- Install one plugin per restart cycle to isolate potential failures.
- Do not modify storage path mappings during plugin installations.
- Take a Proxmox container backup prior to installing untested plugins.

## Troubleshooting

### The container will not start after installing a plugin

Do not keep retrying the restart. Each failed start is another interrupted
database write.

1. Verify the media mount on the host first — a coincidental mount drop looks
   exactly like a plugin failure.
2. If Jellyfin is still reachable, disable or remove the new plugin and its
   repository.
3. If the container stays unstable or the database is corrupt, restore from the
   most recent Proxmox backup before making further plugin changes.

### HTTP 500 under heavy indexing (SQLite error 5)

During bursts of concurrent metadata work or automated API traffic, Jellyfin can
return `HTTP 500` with:

```text
SQLite Error 5: unable to delete/modify user-function due to active statements
```

**This is transient lock contention, not corruption.** Jellyfin's connection
pool hits file contention when a connection reset is attempted while background
threads still hold undisposed data readers open.

**Do not restore from backup for this symptom.** Cycle the container to clear
the deadlocked handles:

```bash
pct stop 201 && pct start 201
```

If `HTTP 500` survives several clean cycles, or the database fails an integrity
check, then treat it as corruption and follow the restore path above.
