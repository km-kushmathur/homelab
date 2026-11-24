# Storage Watchdog Reference

The [storage watchdog](../scripts/check_storage.sh) runs every 15 seconds on the Proxmox host. It verifies the expected read-write ext4 filesystem, its UUID and block device, the marker, and the shared media directories. The check also performs a temporary write and fsync before reporting available space.

## Recovery sequence

1. If verification fails, record which dependent containers are running and shut them down gracefully.
2. Confirm that every dependent container is stopped. If a different filesystem is mounted, leave it mounted for investigation. A stale mount with no resolvable UUID is eligible for clean unmount only if its mount ID, device number, and filesystem type match the last successful check.
3. Mount the configured filesystem through `/etc/fstab` and repeat the full probe. Restart only the containers recorded as running, with another check before each restart and a separate Proxmox pre-start hook.
4. If recovery fails, check the journal and follow the [manual recovery runbook](admin_storage_recovery.md). Start any container left stopped only after verifying storage.

The timer uses `flock` to prevent overlapping runs. A recovery run performs no automatic library rescan.

## Configuration

Deploy the identity file and Proxmox hooks using the [storage setup](setup/02-storage.md) and [container setup](setup/03-lxc-containers.md), then install the timer using the [watchdog setup](setup/09-storage-watchdog.md).

| Setting | Purpose |
| :--- | :--- |
| `/etc/homelab/media-storage.json` | Expected mountpoint and ext4 filesystem UUID |
| `WATCHDOG_CT_IDS` | Space-separated container IDs in `/etc/homelab/homelab.env`, defaulting to `201 204` |
| `STOP_TIMEOUT` | Seconds allowed for each graceful shutdown, defaulting to `30` |
| `MAINTENANCE_LOCK` | Lock path for the watchdog and write-path checks, defaulting to `/tmp/maintenance.lock` |
| `NTFY_BASE_URL` and `NTFY_TOPIC` | Optional notification destination configured in the private environment file |

Use `timeout 20 python3 /opt/homelab/scripts/media_storage.py --json` to check storage directly and `systemctl list-timers storage-watchdog.timer` to confirm scheduling. Use `journalctl -u storage-watchdog.service -n 50` to investigate failures.
