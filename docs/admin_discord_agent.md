# Discord Bridge and SRE Agent

Architecture and operational runbook for [`scripts/discord_bridge.py`](../scripts/discord_bridge.py), running as a systemd service on the hypervisor host.

Setup instructions: [setup/10-discord-agent.md](setup/10-discord-agent.md).

> 🛑 **Security Architecture & Role Management Policy:**
> - **Role management:** Discord role commands let administrators manage lower-ranked members and roles. The configured owner can manage administrator roles.
> - **Permanent Owner Immutability Rule:** The Homelab Owner (`OWNER_DISCORD_ID`) is permanently exempt, untouchable, and immutable. Under NO circumstances may ANY user (Admin or Operator), agent command, or script grant, revoke, modify, strip, elevate, or tamper with the Owner's roles, permissions, channel overwrites, or status. Any attempt to touch the Owner's roles or permissions is immediately and unconditionally refused and alerted to the ops channel.
> - **Channel Privacy:** Operators have zero visibility into any other user's private media request channel (`#req-<username>`) and cannot access the admin `#ops` channel.

> ℹ️ **Note:** The Discord bot token and service credentials live securely in `/etc/homelab/homelab.env` (mode `0600`) and are untracked in version control.

---

## Dual-Plane Architecture

```
                                    +------------------------------------------+
                                    |               Discord Guild              |
                                    +------------------------------------------+
                                         /                                  \
                                        /                                    \
                                       v                                      v
                 +--------------------------------------+      +--------------------------------------+
                 |        Household Intake Plane        |      |          Operator SRE Plane          |
                 | (Private Channels under Category/DM) |      |         (#ops)           |
                 +--------------------------------------+      +--------------------------------------+
                     |              |              |                              |
                     v              v              v                              v
               /open-channel    /request        /status                    !agent  /  /agent
                     |              |              |                              |
                     |              |              |                              v
                     |              |              |                  +-----------------------+
                     |              |              |                  |       Agent CLI       |
                     |              |              |                  |  (Grounded Workspace) |
                     |              |              |                  +-----------------------+
                     v              v              v                              |
           +-----------------------------------------------+                      v
           |             Arr CLI Helper Script             |             Hypervisor Host
           |         (/opt/homelab/scripts/arr_cli.py)     |
           +-----------------------------------------------+
                 |              |              |
                 v              v              v
             Jellyseerr       Radarr         Sonarr
             (Port 5055)   (Port 7878)    (Port 8989)
```

Plane separation is strictly enforced: agent commands are rejected in household channels, and household interactions respond ephemerally to avoid leaking infrastructure details.

---

## Household Intake Commands

### 1. `/open-channel`
- **Purpose:** On-demand provisioning of a private personal media request channel for existing server members.
- **Location:** Created under the configured media requests category.
- **Channel Format:** `#req-<username>` (e.g. `#req-alice`).
- **Permissions:** Private (`@everyone` denied view/send; target user granted full interaction rights; bot and admin full access).
- **Onboarding:** Pins a welcome guide explaining how to search and request media.

### 2. Auto-Provisioning (`on_member_join`)
- Automatically creates the private request channel and sends a direct welcome DM whenever a new member joins the server.

### 3. `/request <title>`
- Performs catalogue search and gates download behind user confirmation.
- **Query parsing:** Resolution tags (`4k`, `1080p`, `720p`, `uhd`, `hdr`, `remux`, `bluray`, `web-dl`), media-type hints, and season tokens (`season 2`, `s02`) are extracted from free-text queries to determine intent while keeping catalogue searches clean.
- **Ranking:** Scored by popularity, vote count, title match, and release year.
- **Release sizing:** Computes runtime-based size estimates (`1080p`: ~35-75 MB/min, `4K`: ~100-185 MB/min).
- **Storage verification:** Request confirmation requires the expected writable ext4 filesystem and at least 15 GiB available. A failed check blocks the request and alerts the ops channel. Use `/storage` for available space and headroom.
- **In-Theatres / CAM Gating:** Releases that are still in theatres without an official digital release are flagged. Users are informed that only theatre recordings exist and instructed to contact an administrator for manual review.

### 4. `/status [user]`
- **User Scope:** In `#req-<user>` channels and DMs, strictly isolates results to the calling user's media. In `#ops`, defaults to all-users with deduplication and requester tags, with an optional user filter.
- **Visual Feedback:** Visual ASCII progress bars (`████░░░░░░`), ETAs, request timestamps (`• <t:timestamp:R>`), and current pipeline states.
- **Pipeline Details Inspector:** Interactive `🔍 See Details` button displays live 5-stage tracking:
  ```text
  [Jellyseerr] ➔ [Radarr/Sonarr] ➔ [Download Client] ➔ [Import] ➔ [Jellyfin]
  ```
  with exact submission timestamps, transfer speeds, sources, and Jellyfin web links.

### 5. `/cancel [title]`
- **Strict User Scoping:** Users and admins can only view and cancel their own active in-progress requests.
- **Safety Gate:** Rejects cancellations for media that has already finished downloading and imported into Jellyfin.
- **Autocomplete & Selection:** Autocompletes active in-progress downloads, with an interactive confirmation view.
- **Clean Teardown:** Deletes Jellyseerr request, removes queue items from Radarr/Sonarr, and purges incomplete files from the download client.

### 6. `/history`
- Interactive request history view displaying past media requests, approval statuses, completion dates, and requester info.

### 7. `/imar`
- Harmless, humorous easter egg slash/prefix command that playfully roasts users for expecting dummy commands to do anything.

---

## Operator SRE Plane

### Channel Scoping & Privilege Tiers
- **Admin Control Plane (`#ops` / `DISCORD_OPS_CHANNEL_ID`):** Restricted to Homelab Owner & Admins. Receives P1/P2 alerts, real-time intake/SRE audit streams, and accepts SRE commands from administrators in that channel and its threads.

### Real-Time Admin Audit Stream (`#ops`)
- **Media Request Searches:** Broadcasts real-time cards when users search for media in `#req-*` channels, capturing raw prompts, target qualities, and matched titles.
- **Security Guardrail Violations:** Instantly alerts `#ops` with admin tags if an operator attempts privilege escalation, role manipulation, or unauthorized execution.

### Server Preservation & Anti-Destruction Guardrails
The gateway strictly blocks operators from executing:
- Filesystem deletion or wiping (`rm -rf` on system/media paths, `mkfs`, `fdisk`, `dd`).
- Destroying or purging Proxmox containers/VMs (`pct destroy`, `qm destroy`).
- Host power control (`shutdown`, `reboot`, `poweroff`).
- Docker volume deletion or stopping containers unprompted (`docker compose down`).
- Dropping databases or wiping media libraries.

---

## Role Management Commands (`/role`)

Homelab Admins can manage server member roles via slash or prefix commands:
- `/role grant <@user> <role>`: Assigns a role (e.g. `Operator`).
- `/role revoke <@user> <role>`: Removes a role.
- `/role list <@user>`: Lists roles assigned to a member.
- **Owner Immutability:** Any attempt to touch the Homelab Owner's roles or permissions is rejected unconditionally.

---

## Autonomous Queue Watchdog & Quality Fallback

The bridge includes an autonomous watchdog loop:
- **Stalled Download Auto-Remediation:** Periodically scans queues for downloads stuck downloading metadata (>900s) or stalled with 0 sources (>600s), blocklists the bad release, purges it from the download client, and triggers an alternative release search.
- **Quality Fallback Prompt View:** If all available indexer releases have 0 active sources, the watchdog automatically delivers an interactive view in the user's `#req-<user>` channel allowing them to switch to an alternative quality with active sources via a dropdown or cancel immediately.

---

## Service Management

```bash
systemctl status homelab-bot.service
systemctl restart homelab-bot.service
journalctl -u homelab-bot.service -n 50 --no-pager
journalctl -u homelab-bot.service -f
```

## Required Bot Permissions

Grant the bot's role **Administrator**, or:
- Manage Channels
- Manage Roles
- View Channels, Send Messages
- Manage Messages
- Explicit permissions on the media requests category
