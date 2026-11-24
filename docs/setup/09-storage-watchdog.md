# 9 — Storage Watchdog

The watchdog monitors the filesystem recorded in [storage setup](02-storage.md) and restarts previously running media containers after a verified recovery. The [Proxmox hooks](03-lxc-containers.md#install-the-storage-startup-hook) protect manual starts as well.

## Deploy

Create the environment file if it does not already exist. Keep existing credentials and local settings when upgrading.

```bash
install -d -m 0750 /etc/homelab
test -e /etc/homelab/homelab.env ||
  install -m 0600 /opt/homelab/scripts/homelab.env.example /etc/homelab/homelab.env
```

Set `WATCHDOG_CT_IDS` to the containers with media bind mounts. Set `NTFY_TOPIC` in the private file if you use [notifications](08-notifications.md).

```bash
timeout 20 python3 /opt/homelab/scripts/media_storage.py --json
install -m 0644 /opt/homelab/config/templates/systemd/storage-watchdog.service /etc/systemd/system/
install -m 0644 /opt/homelab/config/templates/systemd/storage-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now storage-watchdog.timer
```

## Verify

```bash
systemctl list-timers storage-watchdog.timer
systemctl start storage-watchdog.service
journalctl -u storage-watchdog.service -n 20
pct status 201
pct status 204
```

A running timer and successful storage probe confirm basic deployment. Test disk failure and recovery on a disposable setup before relying on automatic recovery. For maintenance and troubleshooting, see the [watchdog reference](../admin_storage_watchdog.md) and [storage recovery runbook](../admin_storage_recovery.md).

**Next** [10 — Discord agent](10-discord-agent.md)
