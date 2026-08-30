# Configuration Templates
 
Production configuration templates with environment variable interpolation:
 
- `docker/` — Media stack compose file and environment contract
- `gluetun/` — WireGuard configuration template for VPN egress
- `tailscale/` — Tailnet ACL policy definition
- `systemd/` — Unit files for Discord bridge daemon and storage watchdog
 
Credentials and host-specific settings belong in `/etc/homelab/homelab.env`. See [`scripts/homelab.env.example`](../../scripts/homelab.env.example) for script variables, and `docker/.env.example` for compose stack variables.
