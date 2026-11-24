# Storage Recovery Runbook

Manual recovery procedures and diagnosis for dropped storage mounts.

For automated recovery, see [Storage Watchdog](admin_storage_watchdog.md).

> **Important:** Stop all containers holding media bind mounts prior to remounting storage. Remounting while containers are active creates stale file handles in the container filesystem.

## Maintenance Mode

The automated watchdog checks mount status every 15 seconds. Create the maintenance lock before performing manual disk operations:

```bash
touch /tmp/maintenance.lock     # suspends watchdog execution
# ... perform maintenance ...
rm -f /tmp/maintenance.lock     # resumes watchdog execution
```

Verify lockfile presence with `ls -l /tmp/maintenance.lock`.

## Manual Recovery Sequence

Enable maintenance mode and check the state of each container with a media bind mount. Gracefully shut down each running container and confirm it has stopped before touching the mount.

```bash
touch /tmp/maintenance.lock
pct status 201
pct status 204
# For each running container:
pct shutdown <ctid> --timeout 60
pct status <ctid>
findmnt -n -M /mnt/media -o TARGET,UUID,FSTYPE,OPTIONS
```

Compare the mounted device with the expected UUID in `/etc/homelab/media-storage.json`. If a different filesystem is mounted or the device cannot be identified, investigate before unmounting. After confirming the device and resolving the underlying issue, cleanly unmount the stale filesystem if it is still mounted, mount it through `/etc/fstab`, and verify it.

```bash
mount /mnt/media
timeout 20 python3 /opt/homelab/scripts/media_storage.py --json
```

After the check succeeds, start the containers that were running before the failure. Check their status, then remove the maintenance lock to resume the watchdog and media intake.

## Ghost Mounts

A disconnected drive can leave a mount entry behind while reads fail with I/O errors. The watchdog can recognize a previously verified mount by mount ID, block device number, and filesystem type. Check the journal and kernel logs if that verification fails.

## Diagnosing "Root folder does not exist"

Missing root folder warnings in Radarr/Sonarr indicate either dropped host storage or container path divergence:

```mermaid
graph TD
    START{"Radarr/Sonarr:<br/>root folder missing"} --> Q1{"Does download client<br/>show existing items<br/>as <b>Missing Files</b>?"}
    Q1 -->|Yes| S1["<b>State 1: Host mount dropped</b><br/>Container sees empty or stale mount."]
    Q1 -->|"No &mdash; downloads<br/>complete normally"| S2["<b>State 2: Path mismatch</b><br/>Mount is healthy; volume mappings diverge."]
    S1 --> F1["Execute manual recovery sequence above"]
    S2 --> F2["Correct compose mappings per admin_application_config.md"]
```

- **State 1:** Host mount dropped. Execute the manual recovery sequence above.
- **State 2:** Mount is healthy but volume paths diverge between containers. Correct path mappings in Docker Compose. See [admin_application_config.md](admin_application_config.md).

## Isolating Failures by Layer

Verify layers sequentially before modifying configurations:

**1. Host layer:**

```bash
ls -la /mnt/media
```

If the directory is empty or missing expected subdirectories, the host mount is down. Execute the manual recovery sequence.

**2. Container layer:**

```bash
pct exec 204 -- ls -la /mnt/media
```

If the host sees media but the container sees an empty directory, restart the container to refresh the bind mount:

```bash
pct stop 204 && pct start 204
```

**3. Docker layer:**

```bash
pct exec 204 -- docker exec radarr ls -la /media
```

If the host and container see media but Docker containers show empty directories, check Docker Compose volume mappings:

```yaml
- ${MEDIA_ROOT}:/media
```

Recreate the stack:

```bash
cd /opt/appdata && docker compose down && docker compose up -d
```

## Host UID Mapping (`101000`)

Host permissions on the media tree should reflect UID `101000`:

```bash
ls -la /mnt/media/downloads
# drwxr-xr-x  101000 101000  ...
```

Unprivileged LXC containers offset host UIDs by `100000`, so host UID `101000` maps to UID `1000` inside the container (the `PUID`/`PGID` used by media containers).

> **Important:** Do not modify ownership to host UID `1000` or apply `chmod 777`. Unprivileged container isolation depends on this mapping.

## Recovery Failure Scenarios

**Container fails to stop within timeout:** Abort remount. Unmounting storage underneath an active container risks filesystem corruption. Inspect container processes before retrying.

**Mount verification fails after cycling:** Leave containers stopped. Check physical connections, power, and kernel logs: `dmesg -T | tail -50`.

**Library rescan fails after mount recovery:** Storage recovery is intact. Trigger library scan manually: `python3 scripts/arr_cli.py jellyfin refresh`.
