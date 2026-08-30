# Manual Media Transfer Runbook

Procedure for manual media ingestion during initial setup or pipeline outages.

> **Note:** Routine additions should use the automated request pipeline to preserve standardized naming, metadata acquisition, and automated library scans.

## Transfer Sequence

**1. Confirm storage mount status:**

```bash
mountpoint -q /mnt/media && echo mounted || mount /mnt/media
ls /mnt/media
```

**2. Stop dependent containers:** Writing into active directory trees during automated library scans can lead to partial imports.

```bash
touch /tmp/maintenance.lock   # suppress storage watchdog
pct stop 201
pct stop 204
pct status 201; pct status 204   # verify both report stopped
```

**3. Ingest files:** Transfer files into existing category directories (`/mnt/media/movies/` or `/mnt/media/tv/`):

| Parameter | Value |
| :--- | :--- |
| Protocol | SFTP / rsync |
| Host | Hypervisor host IP (`192.168.1.10`) |
| Destination | `/mnt/media/movies/` or `/mnt/media/tv/` |

Example transfer via `rsync`:

```bash
rsync -avh --progress /local/path/ root@192.168.1.10:/mnt/media/movies/
```

Do not create alternative top-level directories outside the configured application root folders.

**4. Verify file integrity:**

```bash
stat "/mnt/media/movies/<new file>.mkv"
```

Verify non-zero file sizes and timestamps against the source.

**5. Flush filesystem cache and cycle mount:**

```bash
sync
umount /mnt/media
mount | grep /mnt/media    # must return nothing
sleep 5
mount /mnt/media
```

Flushing via `sync` ensures buffered writes are committed to disk before unmounting.

**6. Restart containers:**

```bash
pct start 201
pct start 204
rm -f /tmp/maintenance.lock
pct status 201; pct status 204   # both: status: running
```

Then trigger a library scan so the media server picks up the new files:

```bash
python3 /opt/homelab/scripts/arr_cli.py jellyfin refresh
```

## Practices

- Copy into existing category directories only.
- Transfer in controlled batches. A single enormous transfer gives you nothing
  to resume from.
- Never disconnect the disk or reboot the host mid-transfer.
- Verify with `stat` before restarting services, not after.
- Keep the maintenance lock in place for the whole operation.

## Troubleshooting

**Power lost mid-transfer.** Assume partial writes. Compare sizes against the
source and re-copy anything that does not match — `rsync` does this for you on a
second run.

**Disk disconnected mid-transfer.** Keep both containers stopped. Follow
[admin_storage_recovery.md](admin_storage_recovery.md) to recover the mount,
verify the files, and only then restart services.

**Files transferred but do not appear in the library.** Check ownership: manual
transfers as `root` produce files owned by host UID `0`, not `101000`. The
services then cannot read them. Fix with:

```bash
chown -R 101000:101000 /mnt/media/movies
```

See the ownership note in
[admin_storage_recovery.md](admin_storage_recovery.md#ownership-101000-is-correct).
