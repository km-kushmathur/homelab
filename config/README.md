# Config Directory Structure

- `templates/` — Deployable configuration templates containing environment variables.
- `examples/` — Reference configurations with placeholder values for host and container settings.

## Templates

| Path | Purpose |
| :--- | :--- |
| `templates/docker/media-stack.compose.yml` | Compose file for the media stack. Fully env-interpolated so no paths or secrets live in Git. |
| `templates/docker/.env.example` | Environment file for the compose stack. Copy to `.env` beside it. |
| `templates/gluetun/wireguard.conf.example` | Annotated reference for a provider-issued WireGuard profile. |
| `templates/tailscale/acl.json` | Tailnet ACL policy. Edit here, then deploy from the Tailscale admin console. |
| `examples/pve/media-storage.json.example` | Expected mountpoint and filesystem UUID for the storage probe and Proxmox startup hook. |
| `templates/systemd/homelab-bot.service` | Unit for the Discord bridge daemon. |
| `templates/systemd/storage-watchdog.{service,timer}` | Unit and 15-second timer for the storage watchdog. |

## Examples

| Path | Purpose |
| :--- | :--- |
| `examples/pve/lxc-jellyfin.conf` | Media server container config, including iGPU passthrough. |
| `examples/pve/lxc-media-stack.conf` | Docker host container config, including the TUN passthrough the VPN container needs. |
| `examples/pve/host-fstab.example` | Host mount table, showing the `nofail` option on the media disk. |

## Shared Media Path Requirement

The download client, Radarr, and Sonarr must all mount the same host directory at the same in-container path (`${MEDIA_ROOT}:/media`).

If mappings diverge (e.g., download client uses `/downloads` while Radarr uses `/media/downloads`), hardlinks cannot be created across distinct paths. The importer will fall back to copying files—doubling storage consumption—or fail with import errors.

## Guarding against drift

Runtime appdata under `/opt/appdata/` is not tracked in Git, so the deployed
stack can drift from these templates without anything flagging it. Diff them
periodically:

```bash
diff -u /opt/appdata/docker-compose.yml config/templates/docker/media-stack.compose.yml
```

Any volume mapping in the live file that does not appear in the template is
drift and should be corrected against
[docs/admin_application_config.md](../docs/admin_application_config.md).
