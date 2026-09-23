# Documentation

Start here: [Setup Guide](setup/01-proxmox-host.md).

Document conventions:
- `admin_*` — Operator runbooks and troubleshooting procedures.
- `user_*` — Household member guides for requesting and streaming media.

## Setup Guide

| | |
| :--- | :--- |
| [01 — Proxmox host](setup/01-proxmox-host.md) | Install hypervisor, configure network, and apply base hardening |
| [02 — Storage](setup/02-storage.md) | Format and mount media disk with `nofail` |
| [03 — Containers](setup/03-lxc-containers.md) | Unprivileged LXCs, bind mounts, and UID mapping |
| [04 — Media server](setup/04-jellyfin.md) | Jellyfin deployment with hardware transcoding |
| [05 — Media stack](setup/05-media-stack.md) | Docker-in-LXC, VPN egress, and path parity |
| [06 — First-run config](setup/06-first-run-config.md) | Service integration and quality profiles |
| [07 — Remote access](setup/07-remote-access.md) | Tailscale mesh network and restrictive ACLs |
| [08 — Notifications](setup/08-notifications.md) | Push alerts via ntfy |
| [09 — Storage watchdog](setup/09-storage-watchdog.md) | Automated recovery for dropped storage mounts |
| [10 — Discord agent](setup/10-discord-agent.md) | Discord bridge daemon and SRE agent configuration |

## Architecture & Reference

- [architecture.md](architecture.md) — System topology, trust boundaries, and design specifications
- [application_ports.md](application_ports.md) — Port allocation and service network mapping

## Operator Runbooks

- [admin_application_config.md](admin_application_config.md) — Path parity baseline, quality definitions, and import remediation
- [admin_backup_recovery.md](admin_backup_recovery.md) — State backup and disaster recovery procedures
- [admin_discord_agent.md](admin_discord_agent.md) — Discord bridge and SRE agent operations
- [admin_jellyfin_plugins.md](admin_jellyfin_plugins.md) — Jellyfin plugin installation and safe restart procedure
- [admin_manual_media_transfer.md](admin_manual_media_transfer.md) — Manual file transfer and library ingestion
- [admin_notifications.md](admin_notifications.md) — Push notification pipeline and event routing
- [admin_storage_capacity.md](admin_storage_capacity.md) — Storage capacity pre-flight gate, safe pruning, and sizing guidelines
- [admin_storage_recovery.md](admin_storage_recovery.md) — Manual recovery from dropped mounts or stale file handles
- [admin_storage_watchdog.md](admin_storage_watchdog.md) — Storage watchdog configuration and operation
- [admin_subtitles.md](admin_subtitles.md) — Automated English subtitle architecture, Bazarr integration, and sync engine
- [admin_tailscale_network.md](admin_tailscale_network.md) — Tailscale ACL configuration and network security model

## User Guides

- [user_media_requests.md](user_media_requests.md) — Media request submission and status tracking
