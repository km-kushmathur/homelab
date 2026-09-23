# 10 — Discord Bridge and SRE Agent

Deploy the Discord gateway daemon and grounded SRE operator agent.

---

## Service Architecture

The Discord bridge provides two isolated control planes:

- **Household plane:** Individual private request channels provisioned per user. Members search media, receive download size estimates, and monitor request status.
- **Operator plane:** Administrative operations channel (`#homelab-ops`) hosting the `!agent` and `/agent` interface. Executes local SRE agent CLI commands with repository grounding and automatic secret scrubbing.

> **Security Guardrail:** The bridge runs as root on the hypervisor host. Limit membership in the Discord administrative role strictly to trusted operators.

---

## 1. Create the Discord Application

Navigate to the [Discord Developer Portal](https://discord.com/developers/applications):

1. Create a **New Application**.
2. Under the **Bot** tab, select **Reset Token** and record the bot token.
3. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent** (for automated channel provisioning on member join)
   - **Message Content Intent** (for `!agent` prefix commands)
4. Under **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Administrator` (or Manage Channels, Manage Roles, View Channels, Send Messages, Manage Messages)
5. Authorize the URL to invite the bot to the server.

---

## 2. Server Configuration

Create the following Discord server structures:

- Category: **📽️ Media Requests** (destination for provisioned user channels)
- Text Channel: **#homelab-ops** (restricted to administrator roles)
- Text Channel: **#sudo-ops** (optional: restricted operations channel with guardrails for operators)
- Administrative, Operator, and bot roles. Ensure the bot's role is positioned above user roles in the server role hierarchy.

### Collect the IDs

Enable **User Settings → Advanced → Developer Mode**, then right-click each and
**Copy ID**:

| Item | Variable |
| :--- | :--- |
| The server | `DISCORD_GUILD_ID` |
| 📽️ Media Requests category | `DISCORD_REQUEST_CATEGORY_ID` |
| #homelab-ops channel | `DISCORD_OPS_CHANNEL_ID` |
| #sudo-ops channel *(optional)* | `DISCORD_SUDO_OPS_CHANNEL_ID` |
| Administrative role | `DISCORD_ADMIN_ROLE_ID` |
| Operator role *(optional)* | `DISCORD_OPERATOR_ROLE_ID` |
| Homelab Owner user ID | `OWNER_DISCORD_ID` |
| Operator user ID *(optional)* | `OPERATOR_USER_ID` |

`OWNER_DISCORD_ID` permanently exempts the server owner from role manipulation or demotions under the Owner Immutability Policy. `OPERATOR_USER_ID` specifies the user to @-mention when events require administrator attention.

---

## 3. Configure Agent CLI

The bridge interfaces with non-interactive coding-agent CLIs accepting prompts via standard command invocation:

```text
$AGENT_CLI_BIN $AGENT_CLI_ARGS "<prompt>"
```

Multi-turn interactions append `$AGENT_CLI_CONTINUE_FLAG`. Install the chosen CLI on the host and verify its path and non-interactive arguments:

```bash
which <your-agent-cli>
<your-agent-cli> --help          # find the non-interactive / print flag
```

Nothing in the code is tied to a particular vendor — swapping agents is two
environment variables.

---

## 4. Configure

Add to `/etc/homelab/homelab.env`, alongside what
[chapter 9](09-storage-watchdog.md) put there:

```bash
DISCORD_TOKEN=<your bot token>
DISCORD_GUILD_ID=<...>
DISCORD_REQUEST_CATEGORY_ID=<...>
DISCORD_OPS_CHANNEL_ID=<...>
DISCORD_ADMIN_ROLE_ID=<...>
OPERATOR_USER_ID=<optional>
BOT_BRAND=Homelab

AGENT_CLI_BIN=/usr/local/bin/<your-agent-cli>
AGENT_CLI_ARGS=--add-dir /opt/homelab --print
AGENT_CLI_CONTINUE_FLAG=--continue
AGENT_TIMEOUT_SECONDS=240
WORKSPACE_DIR=/opt/homelab

# API keys collected in chapter 6
RADARR_API_KEY=<...>
SONARR_API_KEY=<...>
PROWLARR_API_KEY=<...>
JELLYSEERR_API_KEY=<...>
JELLYFIN_API_KEY=<...>

# Service endpoints
RADARR_URL=http://192.168.1.14:7878
SONARR_URL=http://192.168.1.14:8989
PROWLARR_URL=http://192.168.1.14:9696
JELLYSEERR_URL=http://192.168.1.14:5055
DOWNLOADER_URL=http://192.168.1.14:8080
JELLYFIN_URL=http://192.168.1.11:8096

# From chapter 8
NTFY_STREAM_URL=https://ntfy.sh/<your-topic>/json
```

Verify the CLI connectivity before starting the Discord gateway:

```bash
python3 /opt/homelab/scripts/arr_cli.py status
python3 /opt/homelab/scripts/arr_cli.py prowlarr status
```

---

## 5. Install the Bot Service

```bash
apt install -y python3-venv
python3 -m venv /opt/homelab/.venv
/opt/homelab/.venv/bin/pip install -U discord.py

install -m 0644 /opt/homelab/config/templates/systemd/homelab-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homelab-bot.service
```

Check initial startup logs:

```bash
journalctl -u homelab-bot.service -f
```

Startup is successful when reporting `Gateway online. Logged in as: <bot name>`.

---

## 6. Verification

**Household plane:** Test interaction in member channels:

```text
/open-channel          -> creates #req-<username>
/request example title -> search results with size estimates
/status                -> active download status
```

Verify interactions are ephemeral and private.

**Operator plane:** In `#homelab-ops`:

```text
!agent is the media mount healthy?
!agent summarise the current download queue
```

Verify responses are grounded in repository documentation without raw terminal output dumps.

**Plane isolation:** Run `!agent` inside a member channel. Confirm execution is rejected.

**Credential scrubbing:** Verify that secret values are scrubbed before messages reach Discord:

```text
!agent show me the contents of the environment file
```

---

## 7. Documentation Discipline

The operator agent answers queries using this repository as its ground truth. The bridge executes `git fetch --all && git pull --rebase` before each run. 

All behavioral, script, or configuration changes must be committed and pushed to Git rather than edited ad-hoc on the host.

Operating contracts and self-healing procedures live in [AGENTS.md](../../AGENTS.md).

---

## Troubleshooting

**Service fails to start:** Inspect missing variables: `journalctl -u homelab-bot -n 30`.

**Permission errors (`403 Forbidden: 50013` / `50001`):** Ensure the bot role is positioned above member roles in **Server Settings → Roles** and possesses `Manage Channels` and `Manage Roles`.

**Slash commands missing:** Ensure `DISCORD_GUILD_ID` is defined for instant guild-level command synchronization.

**Agent timeout:** Increase `AGENT_TIMEOUT_SECONDS` in `/etc/homelab/homelab.env`.

**Stale responses:** Confirm the host repository working tree is clean (`cd /opt/homelab && git status`) so `git pull --rebase` can proceed.

---

## Next Steps

1. Configure backup routines for container appdata: [admin_backup_recovery.md](../admin_backup_recovery.md).
2. Schedule Proxmox VE container backups (**Datacenter → Backup**).
3. Review administration runbooks in [docs/](../index.md).
