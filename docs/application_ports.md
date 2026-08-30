# Application Ports

Listening services and default ports across the environment:

| Application | Address | Port | Notes |
| :--- | :--- | :--- | :--- |
| Proxmox VE | `192.168.1.10` | `8006` | Hypervisor web UI (HTTPS) |
| Jellyfin | `192.168.1.11` | `8096` | Media server in CT 201 |
| Game server panel | `192.168.1.12` | `8080` | CT 202 |
| DNS | `192.168.1.13` | — | CT 203, planned |
| Downloader | `192.168.1.14` | `8080` | Web UI published via `gluetun`. Inbound traffic on `6881/tcp` and `6881/udp` |
| Radarr | `192.168.1.14` | `7878` | Movies |
| Sonarr | `192.168.1.14` | `8989` | TV series |
| Prowlarr | `192.168.1.14` | `9696` | Indexer manager |
| FlareSolverr | `192.168.1.14` | `8191` | Challenge-solving proxy |
| Jellyseerr | `192.168.1.14` | `5055` | Request portal |
| ntfy | `192.168.1.14` | `80` | Push notification server |

Port assignments are configurable in [`config/templates/docker/.env.example`](../config/templates/docker/.env.example).

## Port Configuration Notes

- **Downloader network binding:** The download client runs with `network_mode: "service:gluetun"`, sharing Gluetun's network namespace. Port mappings (`8080`, `6881`) are published on the `gluetun` service container definition to enforce kill-switch isolation.
- **Remote access restrictions:** Tailscale ACL policy permits remote members access only to `8096` (Jellyfin) and `5055` (Jellyseerr). Administrative and backend ports remain LAN-only. See [admin_tailscale_network.md](admin_tailscale_network.md).
