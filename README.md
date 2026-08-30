# Homelab

A single-node Proxmox homelab running a media server, automated storage watchdog, download pipeline, and Discord SRE operations agent grounded on this repository.

Configuration templates and documentation use generic placeholders.

Start here: [Setup Guide](docs/setup/01-proxmox-host.md).

---

## System Overview

- **Automated storage recovery:** The library resides on external storage. If the mount drops, running containers could write into the host's root filesystem. A systemd watchdog detects drops every 15 seconds, stops dependent containers, remounts storage, and restarts services. See [Storage Watchdog](docs/admin_storage_watchdog.md).
- **Discord media requests:** Private user channels provide `/request` and `/status` commands with release size estimates and progress updates, isolating household members from direct infrastructure access.
- **Discord operations agent:** An SRE agent in the ops channel executes administrative tasks and troubleshooting commands via hypervisor CLI, grounded on documentation in this repository.

---

## Architecture

```mermaid
graph TB
    subgraph host["Hypervisor host &mdash; Proxmox VE"]
        direction TB
        WD["storage watchdog<br/><i>systemd timer, 15s</i>"]
        BOT["Discord bridge<br/><i>systemd service</i>"]
        DISK[("shared media disk<br/><code>/mnt/media</code>")]
    end

    subgraph cts["Containers"]
        direction LR
        JF["<b>201</b> jellyfin<br/><i>media server</i>"]
        GS["<b>202</b> game-server"]
        DNS["<b>203</b> dns<br/><i>planned</i>"]
        MS["<b>204</b> media-stack<br/><i>Docker</i>"]
    end

    subgraph stack["media-stack containers"]
        direction LR
        VPN["gluetun<br/><i>VPN + kill-switch</i>"]
        DL["download client"]
        RAD["radarr"]
        SON["sonarr"]
        PROW["prowlarr"]
        SEERR["jellyseerr<br/><i>request portal</i>"]
    end

    USERS(["household"]) --> BOT
    USERS --> SEERR
    OPS(["operator"]) --> BOT
    BOT --> MS
    BOT --> JF

    MS --> stack
    SEERR --> RAD & SON
    RAD & SON --> PROW
    RAD & SON --> DL
    DL --> VPN
    VPN --> WAN(["internet"])

    DISK -.mounted into.-> JF
    DISK -.mounted into.-> MS
    WD -.watches.-> DISK
    WD -.restarts.-> JF & MS

    JF --> VIEWERS(["playback"])
```

### Request flow

```mermaid
sequenceDiagram
    autonumber
    participant U as Member
    participant B as Discord bridge
    participant S as Jellyseerr
    participant A as Radarr / Sonarr
    participant I as Prowlarr
    participant D as Download client
    participant M as Jellyfin

    U->>B: /request example title
    B->>S: search catalogue
    S-->>B: metadata + size estimate
    B-->>U: confirm request?
    U->>B: confirm
    B->>S: submit request
    S->>A: hand off
    A->>I: query configured indexers
    I-->>A: candidate releases
    A->>D: send best match (VPN egress)
    D-->>A: complete
    A->>A: import via hardlink into the library
    A-->>B: available
    B-->>U: media available
    M-->>U: stream
```

### Network

```mermaid
graph LR
    NET(["router<br/>192.168.1.1"]) --- BR["<b>vmbr0</b><br/>Linux bridge<br/>192.168.1.10/24"]
    BR --- C1["201 jellyfin<br/>192.168.1.11:8096"]
    BR --- C2["202 game-server<br/>192.168.1.12"]
    BR --- C3["203 dns<br/>192.168.1.13"]
    BR --- C4["204 media-stack<br/>192.168.1.14"]
    TS(["Tailscale<br/><i>remote access</i>"]) -.encrypted tunnel.-> BR
```

Remote access is Tailscale-only — nothing is port-forwarded to the internet.
The tailnet ACL grants members exactly two destinations, the media server and
the request portal, and nothing else. See
[docs/admin_tailscale_network.md](docs/admin_tailscale_network.md).

---

## Hardware Requirements

| Component | Specification |
| :--- | :--- |
| Host | x86-64 system with virtualization support (Intel VT-x / AMD-V) |
| CPU | 4+ threads; Intel iGPU recommended for hardware transcoding |
| RAM | 16 GB |
| System disk | 500 GB SSD for hypervisor and container root filesystems |
| Media disk | Dedicated external or internal storage formatted as ext4 |
| Hypervisor | Proxmox VE 9.x |

---

## Repository Structure

| Path | Contents |
| :--- | :--- |
| [docs/setup/](docs/setup/01-proxmox-host.md) | Step-by-step setup guides |
| [docs/](docs/index.md) | Operational runbooks and troubleshooting guides |
| [docs/architecture.md](docs/architecture.md) | Topology, network scheme, and architecture reference |
| [scripts/](scripts/README.md) | Media stack CLI, Discord bridge, and storage watchdog scripts |
| [config/](config/README.md) | Compose, systemd, ACL, and container configuration templates |
| [AGENTS.md](AGENTS.md) | Grounding contract for the SRE agent |

Application state (`/opt/appdata/`) is untracked; the repository stores configuration templates and documentation.

---

## Critical Requirements

- **Path parity:** All media services must mount the same host directory at the same in-container path (`${MEDIA_ROOT}:/media`). If paths diverge, file imports fall back to copying instead of hardlinking, doubling disk usage, or fail completely.
- **`nofail` mount option:** External storage must be mounted with `nofail` in `/etc/fstab` so the host boots cleanly if the drive is disconnected or slow to initialize.

---

## Compliance and Scope

This repository provides infrastructure templates and runbooks for private media management. No external indexers, feeds, or media assets are configured or distributed. Operators are responsible for configuring authorized services in compliance with applicable licenses.

## License

[MIT](LICENSE).
