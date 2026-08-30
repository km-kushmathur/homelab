# Backup and Recovery Procedures

Application state—including Radarr and Sonarr databases, indexer definitions, quality profiles, and request history—resides in `/opt/appdata/` inside the media-stack container. Because this data represents active runtime state rather than static templates, it cannot be rebuilt from repository configurations.

> **Important:** Perform a verified backup before initiating migrations, software upgrades, or system maintenance.

## Backup Procedure

```bash
# From the hypervisor host
pct enter 204

ls -ld /opt/appdata          # verify directory existence
cd /opt/appdata
```

**1. Stop containers:** Archiving live SQLite databases during active writes can lead to database corruption.

```bash
docker compose down
```

**2. Create archive:** Create a timestamped archive and verify its size:

```bash
cd /opt
BACKUP_NAME="appdata-$(date +%F-%H%M%S).tar.gz"
tar -czf "/root/$BACKUP_NAME" appdata
ls -lh "/root/$BACKUP_NAME"
```

> **Warning:** Do not leave the sole backup archive inside the container. Copy archives to external storage or the hypervisor host.

**3. Transfer archive off-host:**

```bash
scp "/root/$BACKUP_NAME" root@192.168.1.10:/root/
# and/or
cp "/root/$BACKUP_NAME" /mnt/media/backups/
```

**4. Restart stack:**

```bash
cd /opt/appdata
docker compose up -d
docker compose ps
```

## Restore Procedure

```bash
pct enter 204
ls -ld /opt/appdata
```

If `/opt/appdata` exists, stop containers before replacement:

```bash
cd /opt/appdata && docker compose down
```

Stage the archive in `/root`:

```bash
scp root@192.168.1.10:/root/<backup_file>.tar.gz /root/
# or
cp /mnt/media/backups/<backup_file>.tar.gz /root/
```

Extract into `/opt`:

```bash
tar -xzf /root/<backup_file>.tar.gz -C /opt
ls -la /opt/appdata
```

Start the media stack:

```bash
cd /opt/appdata
docker compose up -d
docker compose ps
```

## Best Practices

- Always shut down containers prior to archiving SQLite databases.
- Use ISO timestamp naming conventions for archives.
- Store backups in at least two separate failure domains (e.g., host root and external storage).
- Validate archive integrity (`tar -tzf <archive>`) and non-zero size before confirming backups.
- Periodically test restoration into an isolated test environment.

## Troubleshooting

**Backup failed partway:** Discard partial or truncated archives. Rerun the backup procedure from the container shutdown step.

**Extraction fails during restore:** Keep containers stopped. Verify archive transfer integrity against source size (`ls -lh`) before re-extracting.

**Stack fails to start after restore:** Inspect `docker compose logs`. Verify media mount status and check that ownership (`101000:101000` on host, `1000:1000` in container) was preserved.

## VPN Kill-Switch Recovery After Network Interruption

A network drop or router reboot can leave Gluetun's WireGuard interface in an unrecoverable state, causing healthcheck failures:

```text
ERROR [vpn] getting public IP address information: all fetchers failed
```

While other LAN traffic functions, Gluetun's kill-switch blocks all downloader and indexer network traffic.

**Remediation:** A simple container restart is insufficient due to stale network state. Tear down the container network namespace completely:

```bash
pct enter 204
cd /opt/appdata
docker compose down

# Force removal if containers hang on shutdown
docker rm -f gluetun downloader radarr sonarr prowlarr flaresolverr jellyseerr

docker compose up -d
docker compose ps
```

Allow approximately 30 seconds for tunnel negotiation and peer re-establishment.
