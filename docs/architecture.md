# Architecture

System topology, naming schemes, and design specifications. Setup procedures: [Setup Guide](setup/01-proxmox-host.md).

## Naming Conventions

Container IDs and IP addresses are assigned independently and recorded in [application_ports.md](application_ports.md). Independent numbering avoids coupling container IDs to network subnets.

Service names used across the system:

| Name | Container | Role |
| :--- | :--- | :--- |
| `jellyfin` | 201 | Media server, hardware transcoding |
| `game-server` | 202 | Game server panel |
| `dns` | 203 | Local DNS filtering *(planned, not deployed)* |
| `media-stack` | 204 | Docker host for the acquisition and request services |
| `homelab-bot` | *host* | systemd service running the Discord bridge |

## Topology

```mermaid
graph TB
    subgraph host["Hypervisor &mdash; Proxmox VE 9.x"]
        direction TB
        PVE["<b>pve</b><br/>192.168.1.10<br/><i>web UI :8006</i>"]
        DISK[("media disk<br/><code>/mnt/media</code><br/><i>ext4, nofail</i>")]
        SYS["<b>systemd</b><br/>homelab-bot.service<br/>storage-watchdog.timer"]
    end

    subgraph containers["Unprivileged LXC containers"]
        JF["<b>201 jellyfin</b><br/>192.168.1.11<br/>2 cores &middot; 8 GB<br/><i>iGPU passthrough</i>"]
        GS["<b>202 game-server</b><br/>192.168.1.12<br/>4 cores &middot; 8 GB"]
        DN["<b>203 dns</b><br/>192.168.1.13<br/><i>planned</i>"]
        MS["<b>204 media-stack</b><br/>192.168.1.14<br/>2 cores &middot; 4 GB<br/><i>nesting + keyctl</i>"]
    end

    PVE --> JF & GS & DN & MS
    DISK -->|"bind mount"| JF
    DISK -->|"bind mount"| MS
    SYS -.->|"monitors, recovers"| DISK

    style DN stroke-dasharray: 5 5
```

`203 dns` is reserved but not deployed.

## Storage

Unified filesystem and path hierarchy across services:

```mermaid
graph LR
    D[("media disk")] --> M["<code>/mnt/media</code><br/><i>host</i>"]
    M -->|bind| J["<code>/mnt/media</code><br/><i>201 jellyfin</i>"]
    M -->|bind| S["<code>/mnt/media</code><br/><i>204 media-stack</i>"]
    S -->|docker volume| C["<code>/media</code><br/><i>every media container</i>"]
    C --> C1["<code>/media/downloads</code>"]
    C --> C2["<code>/media/movies</code>"]
    C --> C3["<code>/media/tv</code>"]
```

**Unified path requirement:** Hardlinking functions only within a single filesystem where both the writer and importer resolve identical paths. Mismatched paths force fallback to full file copying (doubling disk usage) or cause import errors. See [admin_application_config.md](admin_application_config.md).

**`nofail` mount option:** External storage uses the `nofail` option in `/etc/fstab` so the host boots cleanly if the drive is disconnected or slow to initialize.

**UID ownership:** The media directory is owned by UID `101000` on the host, which maps to UID `1000` inside unprivileged containers. Incorrect host ownership causes permission errors inside containers. See [setup/03-lxc-containers.md](setup/03-lxc-containers.md).

## SRE Agent Architecture

Administrative management-plane component, isolated from household channels.

```mermaid
sequenceDiagram
    autonumber
    participant O as Operator
    participant B as Discord bridge<br/>(systemd, host)
    participant G as git
    participant A as Agent CLI
    participant H as Homelab

    O->>B: !agent why is the queue stuck?
    B->>B: check admin role
    B->>G: fetch + rebase /opt/homelab
    Note over G: docs are the agent's context,<br/>so they are refreshed first
    B->>A: run, grounded on /opt/homelab
    A->>H: read-only inspection via arr_cli.py
    H-->>A: state
    A-->>B: natural-language answer
    B->>B: scrub credentials, chunk to 1900 chars
    B-->>O: reply
```

**Design specifications:**

- **Documentation grounding:** The agent answers strictly from repository documentation and local runbooks.
- **Automated repository sync:** The bridge pulls the repository prior to each invocation to ensure up-to-date runbook context.

## Trust boundaries

```mermaid
graph TB
    subgraph pub["Untrusted"]
        NET(["internet"])
    end
    subgraph mem["Household plane &mdash; members"]
        RC["private request channels"]
        SEERR["request portal"]
        PLAY["media playback"]
    end
    subgraph ops["Operator plane &mdash; admins only"]
        OPSC["ops channel"]
        AGENT["SRE agent"]
        PVEUI["hypervisor web UI"]
    end

    NET -.->|"Tailscale only,<br/>nothing port-forwarded"| mem
    NET -.->|"VPN egress,<br/>kill-switch on"| ops
    mem -->|"strictly separated"| ops

    style ops stroke-width:2px
```

- Nothing is exposed to the internet by port forwarding. Remote access is
  Tailscale, with an ACL granting members exactly two destinations.
- Download client egress is forced through a VPN container with a kill-switch:
  if the tunnel drops, traffic is blocked rather than falling back to the ISP
  connection.
- Agent commands are restricted to the ops channel, and household command interactions are ephemeral.

## Scope & Non-Goals

- **Manual deployment reference:** Designed as a reference architecture with templates and runbooks rather than Terraform/Ansible automation.
- **Single-node reliability:** High availability clustering is out of scope; resilience is provided via automated storage watchdog and backup procedures.
- **Access tiers:** Two trust domains: household members and system administrators.
