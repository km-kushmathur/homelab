#!/usr/bin/env python3
"""
Homelab Discord gateway bridge daemon.

Runs two planes over one bot:

  Household plane -- ordinary members request media in their own private
  channel, see live progress, and never touch the infrastructure.

  Operator plane  -- administrators run a grounded SRE agent against the
  homelab documentation repo from an ops channel.

Configuration is entirely environment-driven. Copy scripts/homelab.env.example
to /etc/homelab/homelab.env (mode 0600) and fill it in; override the location
with HOMELAB_ENV_FILE. No token, ID, or endpoint is hardcoded: a missing
required value aborts startup with a message naming the variable.
"""

import os
import sys
import time
import re
import json
import asyncio
import shlex
import subprocess
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Union

import discord
from discord import app_commands
from discord.ext import commands


def human_size(bytes_val: int) -> str:
    """Format bytes into human readable string."""
    if bytes_val is None or bytes_val < 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


def format_human_eta(raw_eta: Optional[str]) -> str:
    """Converts raw time strings (e.g. 00:15:30) into friendly human phrases without technical jargon."""
    if not raw_eta or raw_eta in ["N/A", "00:00:00", "0", "None"]:
        return "Estimating time..."
    parts = raw_eta.strip().split(":")
    try:
        if len(parts) == 3:
            hrs = int(parts[0])
            mins = int(parts[1])
            if hrs > 0 and mins > 0:
                return f"~{hrs} hr {mins} mins left"
            elif hrs > 0:
                return f"~{hrs} hr left"
            elif mins > 0:
                return f"~{mins} mins left"
            else:
                return "< 1 min left"
        elif len(parts) == 2:
            mins = int(parts[0])
            return f"~{mins} mins left" if mins > 0 else "< 1 min left"
    except Exception:
        pass
    return f"~{raw_eta} left"


DEFAULT_ENV_FILE = "/etc/homelab/homelab.env"


def load_env_file(filepath: Optional[str] = None) -> None:
    """Loads key-value pairs from an env file into os.environ if not already set."""
    filepath = filepath or os.environ.get("HOMELAB_ENV_FILE", DEFAULT_ENV_FILE)
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to load env file {filepath}: {e}\n")


load_env_file()

# ---------------------------------------------------------------------------
# Configuration
#
# Nothing below has a fallback value. Deploying this bot means writing an
# environment file; there is deliberately no way to run it on someone else's
# credentials by accident, and no secret can end up in version control.
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
DISCORD_REQUEST_CATEGORY_ID = os.getenv("DISCORD_REQUEST_CATEGORY_ID", "")
DISCORD_OPS_CHANNEL_ID = os.getenv("DISCORD_OPS_CHANNEL_ID", "")
DISCORD_ADMIN_ROLE_ID = os.getenv("DISCORD_ADMIN_ROLE_ID", "")

# Optional: the user @-mentioned when something needs operator attention.
# Unset is fine -- the bot then refers to "an administrator" instead.
OPERATOR_USER_ID = os.getenv("OPERATOR_USER_ID", "")

# Name shown in embed footers.
BOT_BRAND = os.getenv("BOT_BRAND", "Homelab")

# ntfy JSON stream the bot subscribes to for real-time request alerts.
NTFY_STREAM_URL = os.getenv("NTFY_STREAM_URL", "")

# Repository clone the SRE agent is grounded against, and the helper CLI inside it.
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/opt/homelab")
CLI_PATH = os.path.join(WORKSPACE_DIR, "scripts", "arr_cli.py")

# Host media mount the bot pre-flight checks before recommending a grab.
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/mnt/media")

# The SRE agent CLI. Any non-interactive coding-agent binary works: the bridge
# shells out to `$AGENT_CLI_BIN $AGENT_CLI_ARGS "<prompt>"` and appends
# AGENT_CLI_CONTINUE_FLAG for multi-turn sessions. See homelab.env.example.
AGENT_CLI_BIN = os.getenv("AGENT_CLI_BIN", "")
AGENT_CLI_ARGS = shlex.split(os.getenv("AGENT_CLI_ARGS", ""))
AGENT_CLI_CONTINUE_FLAG = os.getenv("AGENT_CLI_CONTINUE_FLAG", "--continue")
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "240"))


def parse_id(val: Optional[str]) -> int:
    """Safely parses string IDs into integers."""
    if not val:
        return 0
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        return 0


GUILD_ID = parse_id(DISCORD_GUILD_ID)
REQUEST_CATEGORY_ID = parse_id(DISCORD_REQUEST_CATEGORY_ID)
OPS_CHANNEL_ID = parse_id(DISCORD_OPS_CHANNEL_ID)
ADMIN_ROLE_ID = parse_id(DISCORD_ADMIN_ROLE_ID)
OPERATOR_ID = parse_id(OPERATOR_USER_ID)

# Variables the bot cannot start without, mapped to where you find each one.
REQUIRED_CONFIG = {
    "DISCORD_TOKEN": "Discord Developer Portal -> your app -> Bot -> Reset Token",
    "DISCORD_GUILD_ID": "Right-click your server -> Copy Server ID (Developer Mode on)",
    "DISCORD_REQUEST_CATEGORY_ID": "Right-click the media requests category -> Copy Channel ID",
    "DISCORD_OPS_CHANNEL_ID": "Right-click your ops channel -> Copy Channel ID",
    "DISCORD_ADMIN_ROLE_ID": "Server Settings -> Roles -> right-click your admin role -> Copy Role ID",
    "AGENT_CLI_BIN": "Absolute path to your agent CLI binary (see homelab.env.example)",
}


def check_config() -> None:
    """Aborts startup with a named-variable error rather than a runtime surprise."""
    missing = [k for k in REQUIRED_CONFIG if not os.getenv(k)]
    if not missing:
        return
    env_file = os.environ.get("HOMELAB_ENV_FILE", DEFAULT_ENV_FILE)
    sys.stderr.write(f"Cannot start: {len(missing)} required setting(s) are unset.\n\n")
    for key in missing:
        sys.stderr.write(f"  {key}\n      {REQUIRED_CONFIG[key]}\n")
    sys.stderr.write(
        f"\nSet these in {env_file} (mode 0600) or in the environment.\n"
        "See scripts/homelab.env.example for the full contract.\n"
    )
    sys.exit(2)


def operator_mention() -> str:
    """The operator as an @-mention, or a neutral phrase when none is configured."""
    return f"<@{OPERATOR_ID}>" if OPERATOR_ID else "an administrator"


# Session state tracker: session_key (channel_id/thread_id) -> last active timestamp (float)
session_timestamps: Dict[str, float] = {}
SESSION_TTL_SECONDS = 900.0  # 15 minutes TTL

# Seen NTFY message IDs tracker to prevent duplicate notifications
seen_ntfy_ids: Set[str] = set()

# Known secrets to scrub
KNOWN_SECRETS = [
    os.getenv("DISCORD_TOKEN", ""),
    os.getenv("JELLYFIN_API_KEY", ""),
    os.getenv("RADARR_API_KEY", ""),
    os.getenv("SONARR_API_KEY", ""),
    os.getenv("PROWLARR_API_KEY", ""),
    os.getenv("JELLYSEERR_API_KEY", ""),
]

SCRUB_PATTERNS = [
    # Discord token regex
    (re.compile(r"[\w-]{24,}\.[\w-]{6}\.[\w-]{27,}", re.IGNORECASE), "[REDACTED_DISCORD_TOKEN]"),
    # WireGuard private key regex
    (re.compile(r"(?i)(PrivateKey|PresharedKey)\s*=\s*[A-Za-z0-9+/=]{20,}"), r"\1 = [REDACTED_WIREGUARD_KEY]"),
    # General secret / token / password assignments
    (re.compile(r"(?i)(api[_-]?key|password|secret|auth_token|token)\s*([:=])\s*[\x27\x22]?([A-Za-z0-9_\-+=/]{8,})[\x27\x22]?"), r"\1\2[REDACTED]"),
    # Downloader PBKDF2 hash
    (re.compile(r"@ByteArray\([^)]+\)"), "[REDACTED_HASH]"),
]


def sanitize_input(query: str) -> str:
    """Sanitizes user input: clamps length to 100 chars and strips shell metacharacters."""
    if not query:
        return ""
    cleaned = query.strip()[:100]
    cleaned = re.sub(r"[;&|`$<>()\\/\[\]{}*?~!\n\r\t\x00-\x1f]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def scrub_secrets(text: str) -> str:
    """Scrub sensitive credentials, tokens, and keys from text before publishing to Discord."""
    if not text:
        return text
    for secret in KNOWN_SECRETS:
        if secret and len(secret) > 5:
            text = text.replace(secret, "[REDACTED_SECRET]")
    for pat, repl in SCRUB_PATTERNS:
        text = pat.sub(repl, text)
    return text


def chunk_message(text: str, max_chars: int = 1900) -> List[str]:
    """
    Splits long messages cleanly across Discord's character limit while
    preserving fenced markdown code blocks across chunk boundaries.
    """
    if len(text) <= max_chars:
        return [text]

    lines = text.split("\n")
    chunks: List[str] = []
    current_chunk: List[str] = []
    current_length = 0
    in_code_block = False
    code_block_lang = ""

    for line in lines:
        line_len = len(line) + 1

        if current_chunk and (current_length + line_len > max_chars):
            if in_code_block:
                current_chunk.append("```")
            chunk_str = "\n".join(current_chunk)
            if chunk_str.strip():
                chunks.append(chunk_str)

            current_chunk = []
            current_length = 0
            if in_code_block:
                reopen_tag = f"```{code_block_lang}" if code_block_lang else "```"
                current_chunk.append(reopen_tag)
                current_length = len(reopen_tag) + 1

        # Track markdown code fence state
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_block_lang = line.strip()[3:].strip()
            else:
                in_code_block = False
                code_block_lang = ""

        # Handle extraordinarily long lines by splitting
        if len(line) > max_chars:
            while len(line) > max_chars:
                sub = line[:max_chars]
                line = line[max_chars:]
                current_chunk.append(sub)
                chunk_str = "\n".join(current_chunk)
                chunks.append(chunk_str)
                current_chunk = []
                current_length = 0

        current_chunk.append(line)
        current_length += len(line) + 1

    if current_chunk:
        chunk_str = "\n".join(current_chunk)
        if chunk_str.strip():
            chunks.append(chunk_str)

    return chunks


def get_session_key(channel: Any) -> str:
    """Returns a unique session key for the channel or thread."""
    return str(channel.id)


def should_carry_context(channel: Any) -> bool:
    """Determines whether to append -c (carry context) to the agent CLI invocation."""
    session_key = get_session_key(channel)
    now = time.time()
    is_thread = isinstance(channel, discord.Thread)
    has_active_window = (
        session_key in session_timestamps
        and (now - session_timestamps[session_key]) < SESSION_TTL_SECONDS
    )
    return is_thread or has_active_window


def record_session_activity(channel: Any) -> None:
    """Updates the last active timestamp for the session."""
    session_key = get_session_key(channel)
    session_timestamps[session_key] = time.time()


def evict_session(channel: Any) -> None:
    """Evicts the session key immediately."""
    session_key = get_session_key(channel)
    session_timestamps.pop(session_key, None)


def is_admin_user(user: Union[discord.User, discord.Member]) -> bool:
    """Verifies if the user has the required admin role or administrator permissions."""
    if not hasattr(user, "roles"):
        return False
    if ADMIN_ROLE_ID and any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in user.roles):
        return True
    if getattr(user, "guild_permissions", None) and user.guild_permissions.administrator:
        return True
    return False


def is_request_channel_permitted(channel: Any) -> bool:
    """Verifies if the channel is authorized for media requests and status queries."""
    if channel is None:
        return True
    # DMs are permitted
    if isinstance(channel, discord.DMChannel) or getattr(channel, "guild", None) is None:
        return True
    category_id = getattr(channel, "category_id", 0)
    if REQUEST_CATEGORY_ID and category_id == REQUEST_CATEGORY_ID:
        return True
    return False


def sync_git_repo() -> None:
    """Fetches and pulls latest workspace changes."""
    try:
        subprocess.run(["git", "fetch", "--all"], cwd=WORKSPACE_DIR, capture_output=True, timeout=15)
        subprocess.run(["git", "pull", "--rebase"], cwd=WORKSPACE_DIR, capture_output=True, timeout=15)
    except Exception as e:
        print(f"Git sync error: {e}", flush=True)


# --- Personal Channel Provisioning Helper ---

async def create_user_request_channel(
    guild: discord.Guild,
    member: Union[discord.Member, discord.User],
) -> Optional[discord.TextChannel]:
    """
    Provisions a private personal media request channel for a member under REQUEST_CATEGORY_ID.
    If the channel already exists, returns the existing channel.
    """
    raw_name = re.sub(r"[^a-z0-9-_]", "", member.name.lower())[:20]
    channel_name = f"req-{raw_name}" if raw_name else f"req-user-{member.id}"

    category = guild.get_channel(REQUEST_CATEGORY_ID) if REQUEST_CATEGORY_ID else None
    if not category and REQUEST_CATEGORY_ID:
        category = discord.utils.get(guild.categories, id=REQUEST_CATEGORY_ID)

    # Check if channel already exists under category or guild
    existing_channel = None
    if category and isinstance(category, discord.CategoryChannel):
        existing_channel = discord.utils.get(category.text_channels, name=channel_name)
    if not existing_channel:
        existing_channel = discord.utils.get(guild.text_channels, name=channel_name)

    if existing_channel:
        print(f"Personal request channel already exists for {member.name}: #{existing_channel.name} ({existing_channel.id})", flush=True)
        return existing_channel

    # Set up granular permissions
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
        ),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            use_application_commands=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            embed_links=True,
            read_message_history=True,
        ),
    }

    if ADMIN_ROLE_ID:
        admin_role = guild.get_role(ADMIN_ROLE_ID)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                read_message_history=True,
            )

    try:
        new_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Personal media requests for {member.display_name} (<@{member.id}>)",
        )
        print(f"✨ Created private request channel #{new_channel.name} for {member.name} ({member.id})", flush=True)

        welcome_embed = discord.Embed(
            title="🎬 Welcome to Your Private Media Request Channel!",
            description=(
                f"Hey <@{member.id}>! This is your personal private space to search and request movies and TV shows from the homelab.\n\n"
                "### 🚀 How to Use:\n"
                "• **`/request <title>`** — Search TMDB, view download size estimates, and submit movie/TV requests.\n"
                "• **`/status`** — View the download queue, progress bars, and recent request status.\n"
                "• **`/open-channel`** — Get a direct link to this channel anytime.\n\n"
                "Requests are automatically queued in Jellyseerr and processed by Radarr/Sonarr!"
            ),
            color=discord.Color.blurple(),
        )
        welcome_embed.set_footer(text=f"{BOT_BRAND} • Automated Media Gateway")
        welcome_msg = await new_channel.send(content=f"Welcome <@{member.id}>!", embed=welcome_embed)
        try:
            await welcome_msg.pin()
        except Exception as pin_err:
            print(f"Warning: Could not pin welcome message in #{new_channel.name}: {pin_err}", flush=True)

        return new_channel

    except discord.errors.Forbidden as f_err:
        print(f"❌ Discord Permission Error ({f_err}): Bot lacks 'Administrator' or category permissions in category {REQUEST_CATEGORY_ID}.", flush=True)
        raise f_err
    except Exception as e:
        print(f"❌ Failed to create request channel for {member.name}: {e}", flush=True)
        raise e


# --- Interactive UI Views with Real Size & Dynamic Media Type Confirmation Gate ---

def build_media_embed(
    item_title: str,
    year: str,
    overview: str,
    media_type: str,
    tmdb_id: int,
    poster_url: Optional[str],
    size_estimates: Dict[str, Any],
    real_releases: Dict[str, Any],
    detail_json: Dict[str, Any],
) -> discord.Embed:
    avg_1080 = size_estimates.get("1080p_avg_gb", 3.5)
    max_1080 = size_estimates.get("1080p_max_gb", 7.5)
    avg_4k = size_estimates.get("4k_avg_gb", 10.0)
    max_4k = size_estimates.get("4k_max_gb", 18.5)

    size_lines = []
    if media_type == "tv":
        num_seasons = detail_json.get("numberOfSeasons", 1)
        num_episodes = detail_json.get("numberOfEpisodes", 10)
        per_ep_1080 = size_estimates.get("per_episode_1080p_gb", 1.4)
        per_ep_4k = size_estimates.get("per_episode_4k_gb", 4.2)
        size_lines.append(f"• **1080p (Full Series):** ~`{avg_1080:.1f} GB` (~`{per_ep_1080:.1f} GB`/ep)")
        size_lines.append(f"• **4K UHD (Full Series):** ~`{avg_4k:.1f} GB` (~`{per_ep_4k:.1f} GB`/ep)")
        size_lines.append(f"• **Show Structure:** `{num_seasons}` Season(s), `{num_episodes}` Episode(s)")
    else:
        rt = detail_json.get("runtime") or detail_json.get("computed_runtime_minutes") or 110
        size_lines.append(f"• **1080p Standard:** ~`{avg_1080:.1f} GB` (Max Cap: `{max_1080:.1f} GB`)")
        size_lines.append(f"• **4K / 2160p UHD:** ~`{avg_4k:.1f} GB` (Max Cap: `{max_4k:.1f} GB`)")
        size_lines.append(f"• **Runtime:** `{rt}` minutes")

    size_block = "\n".join(size_lines)

    embed = discord.Embed(
        title=f"{item_title}{year}",
        description=(
            f"{overview}\n\n"
            f"📊 **Download Sizes & Profiles:**\n{size_block}\n\n"
            f"💡 *Click **Interactive Release Search** below to inspect live tracker releases and exact file sizes.*"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Media Type", value="🎬 Movie" if media_type == "movie" else "📺 TV Show", inline=True)
    embed.add_field(name="TMDB ID", value=str(tmdb_id), inline=True)

    media_info = detail_json.get("mediaInfo") or {}
    status_map = {1: "UNKNOWN", 2: "PENDING", 3: "PROCESSING", 4: "PARTIAL", 5: "AVAILABLE"}
    status_str = status_map.get(media_info.get("status", 1), "NOT REQUESTED")
    embed.add_field(name="Library Status", value=status_str, inline=True)

    if poster_url:
        embed.set_thumbnail(url=poster_url)
    embed.set_footer(text="Click a button below to choose quality and confirm download")
    return embed


class MediaRequestConfirmationView(discord.ui.View):
    """Confirmation View presenting download size and quality confirmation before triggering request."""

    def __init__(
        self,
        media_id: int,
        title: str,
        target_type: str,
        quality: str,
        embed: discord.Embed,
        is_4k: bool = False,
        seasons: str = "all",
        initial_embed: Optional[discord.Embed] = None,
        size_estimates: Optional[Dict[str, Any]] = None,
        real_releases: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(timeout=120)
        self.media_id = media_id
        self.title = title
        self.target_type = target_type
        self.quality = quality
        self.embed = embed
        self.is_4k = is_4k
        self.seasons = seasons
        self.initial_embed = initial_embed or embed
        self.size_estimates = size_estimates or {}
        self.real_releases = real_releases or {}

    @discord.ui.button(label="Confirm Download", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def confirm_download(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Pre-flight Storage Check: Verify host has enough storage for user request
        import shutil
        try:
            total_b, used_b, free_b = shutil.disk_usage(MEDIA_ROOT)
            free_gb = free_b / (1024 ** 3)
            if free_gb < 15.0:
                self.embed.color = discord.Color.orange()
                operator = operator_mention()
                self.embed.add_field(
                    name="📦 Server Storage is Full",
                    value=(
                        f"The server is currently low on storage space, so your request for **{self.title}** couldn't start yet.\n\n"
                        f"Please contact {operator} to free up some space."
                    ),
                    inline=False,
                )
                await interaction.edit_original_response(embed=self.embed, view=None)

                # Raise it in the ops channel
                if OPS_CHANNEL_ID and interaction.guild:
                    ops_ch = interaction.guild.get_channel(OPS_CHANNEL_ID)
                    if ops_ch:
                        alert = discord.Embed(
                            title="🚨 Storage Capacity Alert",
                            description=(
                                f"• **User:** <@{interaction.user.id}> (`{interaction.user.name}`)\n"
                                f"• **Requested:** {self.title} (`{self.target_type.upper()}`)\n"
                                f"• **Available Space:** `{free_gb:.1f} GB free` on `{MEDIA_ROOT}` (Below 15 GB safe threshold)\n"
                                f"• **Action Required:** Clear host media drive space."
                            ),
                            color=discord.Color.red(),
                        )
                        await ops_ch.send(content=f"⚠️ {operator} **Storage Full Alert!**", embed=alert)
                return
        except Exception:
            pass

        try:
            cmd = [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "jellyseerr",
                "request",
                "--media-type",
                self.target_type,
                "--id",
                str(self.media_id),
                "--quality",
                self.quality,
            ]
            if self.is_4k:
                cmd.append("--is-4k")
            if self.target_type == "tv" and self.seasons:
                cmd.extend(["--seasons", str(self.seasons)])

            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=WORKSPACE_DIR,
                shell=False,
            )

            res = {}
            if proc.stdout:
                try:
                    res = json.loads(proc.stdout)
                except Exception:
                    pass

            if proc.returncode == 0 and not res.get("error"):
                req_id = res.get("id", "N/A")
                q_badge = "✨ 4K Ultra-HD" if self.is_4k else "🎬 Standard HD (1080p)"
                season_note = f" (Seasons: {self.seasons})" if self.target_type == "tv" and self.seasons != "all" else ""
                self.embed.color = discord.Color.green()
                self.embed.add_field(
                    name="Request Status",
                    value=f"✅ Successfully queued request **#{req_id}** for **{self.title}**{season_note} in **{q_badge}**!",
                    inline=False,
                )
                await interaction.edit_original_response(embed=self.embed, view=None)

                # Notify the operator in the ops channel
                if OPS_CHANNEL_ID and interaction.guild:
                    ops_ch = interaction.guild.get_channel(OPS_CHANNEL_ID)
                    if ops_ch:
                        try:
                            tag_text = f"🔔 {operator_mention()} **New Media Request Submitted!**" if OPERATOR_ID else "🔔 **New Media Request Submitted!**"
                            admin_embed = discord.Embed(
                                title=f"🍿 New Request: {self.title}",
                                description=(
                                    f"• **Media:** {self.title} (`{self.target_type.upper()}`)\n"
                                    f"• **Quality Profile:** `{q_badge}`\n"
                                    f"• **Requester:** <@{interaction.user.id}> (`{interaction.user.name}`)\n"
                                    f"• **Channel:** <#{interaction.channel_id}>\n"
                                    f"• **Jellyseerr Request:** `#{req_id}` (TMDB ID: `{self.media_id}`)"
                                ),
                                color=discord.Color.green(),
                            )
                            admin_embed.set_footer(text="Homelab Automated Intake Alert • Discord Gateway")
                            await ops_ch.send(content=tag_text, embed=admin_embed)
                        except Exception as tag_err:
                            print(f"Warning: Could not post admin alert to the ops channel: {tag_err}", flush=True)

            else:
                err_msg = res.get("message") or res.get("response") or proc.stderr or "Request submission failed"
                self.embed.color = discord.Color.red()
                self.embed.add_field(
                    name="Request Status",
                    value=f"❌ Error submitting request: `{err_msg}`",
                    inline=False,
                )
                await interaction.edit_original_response(embed=self.embed, view=None)

        except Exception as e:
            self.embed.color = discord.Color.red()
            self.embed.add_field(
                name="Request Status",
                value=f"❌ Error: `{str(e)}`",
                inline=False,
            )
            await interaction.edit_original_response(embed=self.embed, view=None)

    @discord.ui.button(label="« Overview", style=discord.ButtonStyle.secondary, emoji="🎬", row=0)
    async def back_to_overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MediaRequestInitialView(
            media_id=self.media_id,
            title=self.title,
            media_type=self.target_type,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
            embed=self.initial_embed,
        )
        await interaction.response.edit_message(embed=self.initial_embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", row=0)
    async def cancel_download(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.embed.color = discord.Color.dark_grey()
        self.embed.add_field(
            name="Request Status",
            value="❌ Request cancelled. No media was added to the queue.",
            inline=False,
        )
        await interaction.response.edit_message(embed=self.embed, view=None)


class SpecificReleaseConfirmationView(discord.ui.View):
    """Confirmation View for a specific chosen release, with 2-way back navigation."""

    def __init__(
        self,
        media_id: int,
        title: str,
        target_type: str,
        release: Dict[str, Any],
        size_estimates: Dict[str, Any],
        real_releases: Dict[str, Any],
        releases_embed: discord.Embed,
        original_embed: discord.Embed,
        is_4k: bool = False,
    ):
        super().__init__(timeout=180)
        self.media_id = media_id
        self.title = title
        self.target_type = target_type
        self.release = release
        self.size_estimates = size_estimates
        self.real_releases = real_releases
        self.releases_embed = releases_embed
        self.original_embed = original_embed
        self.is_4k = is_4k
        self.quality = "4k" if is_4k else "1080p"

    @discord.ui.button(label="Confirm Download", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def confirm_download(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Pre-flight Storage Check
        import shutil
        try:
            total_b, used_b, free_b = shutil.disk_usage(MEDIA_ROOT)
            free_gb = free_b / (1024 ** 3)
            if free_gb < 15.0:
                operator = operator_mention()
                err_embed = discord.Embed(
                    title="📦 Server Storage is Full",
                    description=(
                        f"The server is currently low on storage space, so your request for **{self.title}** couldn't start yet.\n\n"
                        f"Please contact {operator} to free up some space."
                    ),
                    color=discord.Color.orange(),
                )
                await interaction.edit_original_response(embed=err_embed, view=None)

                if OPS_CHANNEL_ID and interaction.guild:
                    ops_ch = interaction.guild.get_channel(OPS_CHANNEL_ID)
                    if ops_ch:
                        alert = discord.Embed(
                            title="🚨 Storage Capacity Alert",
                            description=(
                                f"• **User:** <@{interaction.user.id}> (`{interaction.user.name}`)\n"
                                f"• **Requested:** {self.title} (`{self.target_type.upper()}`)\n"
                                f"• **Available Space:** `{free_gb:.1f} GB free` on `{MEDIA_ROOT}` (Below 15 GB safe threshold)\n"
                                f"• **Action Required:** Clear host media drive space."
                            ),
                            color=discord.Color.red(),
                        )
                        await ops_ch.send(content=f"⚠️ {operator} **Storage Full Alert!**", embed=alert)
                return
        except Exception:
            pass

        try:
            cmd = [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "jellyseerr",
                "request",
                "--media-type",
                self.target_type,
                "--id",
                str(self.media_id),
                "--quality",
                self.quality,
            ]
            if self.is_4k:
                cmd.append("--is-4k")

            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=WORKSPACE_DIR,
                shell=False,
            )

            res = {}
            if proc.stdout:
                try:
                    res = json.loads(proc.stdout)
                except Exception:
                    pass

            if proc.returncode == 0 and not res.get("error"):
                req_id = res.get("id", "N/A")
                q_label = self.release.get("quality_label", "HD")
                rel_t = self.release.get("title", "Selected Release")
                sz_val = self.release.get("size_gb", 0)

                success_embed = discord.Embed(
                    title=f"✅ Download Queued: {self.title}",
                    description=(
                        f"Successfully queued request **#{req_id}** in Jellyseerr / Radarr!\n\n"
                        f"• **Target Media:** `{self.title}` (`{self.target_type.upper()}`)\n"
                        f"• **Target Quality:** `{q_label}`\n"
                        f"• **Selected Release:** `{rel_t}`\n"
                        f"• **Exact Size:** `{sz_val:.2f} GB`\n"
                        f"• **Tracker:** `{self.release.get('indexer', 'Tracker')}` ({self.release.get('seeders', 0)} seeds)"
                    ),
                    color=discord.Color.green(),
                )
                success_embed.set_footer(text=f"{BOT_BRAND} • Automated Download Intake")
                await interaction.edit_original_response(embed=success_embed, view=None)

                # Notify the operator in the ops channel
                if OPS_CHANNEL_ID and interaction.guild:
                    ops_ch = interaction.guild.get_channel(OPS_CHANNEL_ID)
                    if ops_ch:
                        try:
                            tag_text = f"🔔 {operator_mention()} **New Media Request Submitted!**" if OPERATOR_ID else "🔔 **New Media Request Submitted!**"
                            admin_embed = discord.Embed(
                                title=f"🍿 New Request: {self.title}",
                                description=(
                                    f"• **Media:** {self.title} (`{self.target_type.upper()}`)\n"
                                    f"• **Quality Profile:** `{q_label}`\n"
                                    f"• **Selected Release:** `{rel_t}` (`{sz_val:.2f} GB`)\n"
                                    f"• **Requester:** <@{interaction.user.id}> (`{interaction.user.name}`)\n"
                                    f"• **Channel:** <#{interaction.channel_id}>\n"
                                    f"• **Jellyseerr Request:** `#{req_id}` (TMDB ID: `{self.media_id}`)"
                                ),
                                color=discord.Color.green(),
                            )
                            admin_embed.set_footer(text="Homelab Automated Intake Alert • Discord Gateway")
                            await ops_ch.send(content=tag_text, embed=admin_embed)
                        except Exception as tag_err:
                            print(f"Warning: Could not post admin alert to the ops channel: {tag_err}", flush=True)

            else:
                err_msg = res.get("message") or res.get("response") or proc.stderr or "Request submission failed"
                err_embed = discord.Embed(
                    title=f"❌ Request Error: {self.title}",
                    description=f"Error submitting request: `{err_msg}`",
                    color=discord.Color.red(),
                )
                await interaction.edit_original_response(embed=err_embed, view=None)

        except Exception as e:
            err_embed = discord.Embed(
                title=f"❌ Request Error: {self.title}",
                description=f"Unexpected error: `{str(e)}`",
                color=discord.Color.red(),
            )
            await interaction.edit_original_response(embed=err_embed, view=None)

    @discord.ui.button(label="« Back to Releases", style=discord.ButtonStyle.secondary, emoji="↩️", row=0)
    async def back_to_releases(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = LiveReleasesView(
            media_id=self.media_id,
            title=self.title,
            media_type=self.media_type,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
            original_embed=self.original_embed,
            releases_embed=self.releases_embed,
        )
        await interaction.response.edit_message(embed=self.releases_embed, view=view)

    @discord.ui.button(label="« Overview", style=discord.ButtonStyle.secondary, emoji="🎬", row=0)
    async def back_to_overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MediaRequestInitialView(
            media_id=self.media_id,
            title=self.title,
            media_type=self.media_type,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
            embed=self.original_embed,
        )
        await interaction.response.edit_message(embed=self.original_embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", row=0)
    async def cancel_download(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_embed = self.original_embed.copy()
        cancel_embed.color = discord.Color.dark_grey()
        cancel_embed.add_field(
            name="Request Status",
            value="❌ Request cancelled. No media was added to the queue.",
            inline=False,
        )
        await interaction.response.edit_message(embed=cancel_embed, view=None)


class LiveReleasesView(discord.ui.View):
    """View showing live tracker releases with a specific release selector and quick download buttons."""

    def __init__(
        self,
        media_id: int,
        title: str,
        media_type: str,
        size_estimates: Dict[str, Any],
        real_releases: Dict[str, Any],
        original_embed: discord.Embed,
        releases_embed: Optional[discord.Embed] = None,
    ):
        super().__init__(timeout=180)
        self.media_id = media_id
        self.title = title
        self.media_type = media_type.lower()
        self.size_estimates = size_estimates
        self.real_releases = real_releases
        self.original_embed = original_embed
        self.releases_embed = releases_embed

        top_releases = self.real_releases.get("top_releases", [])

        # 1. Interactive Release Select Menu (Row 0)
        if top_releases:
            options = []
            for idx, r in enumerate(top_releases[:8]):
                q_lbl = r.get("quality_label", "HD")[:20]
                sz_gb = r.get("size_gb", 0)
                seeds = r.get("seeders", 0)
                idx_name = r.get("indexer", "Tracker")[:14]
                rel_title = r.get("title", "")[:40]

                label = f"{idx+1}. [{q_lbl}] {sz_gb:.2f} GB"[:95]
                desc = f"{seeds} Seeds • {idx_name} • {rel_title}"[:95]
                options.append(discord.SelectOption(label=label, value=str(idx), description=desc))

            select = discord.ui.Select(placeholder="🎯 Select a specific release to download...", options=options, row=0)
            select.callback = self.on_select_release
            self.add_item(select)

        # 2. Quick Download Action Buttons (Row 1)
        if self.media_type == "movie":
            btn_1080 = discord.ui.Button(label="Request 1080p Profile", style=discord.ButtonStyle.primary, emoji="🎬", row=1)
            btn_1080.callback = self.download_1080
            self.add_item(btn_1080)

            btn_4k = discord.ui.Button(label="Request 4K Profile", style=discord.ButtonStyle.success, emoji="✨", row=1)
            btn_4k.callback = self.download_4k
            self.add_item(btn_4k)
        else:
            btn_1080 = discord.ui.Button(label="Request TV (1080p)", style=discord.ButtonStyle.primary, emoji="📺", row=1)
            btn_1080.callback = self.download_1080
            self.add_item(btn_1080)

            btn_4k = discord.ui.Button(label="Request TV (4K)", style=discord.ButtonStyle.success, emoji="✨", row=1)
            btn_4k.callback = self.download_4k
            self.add_item(btn_4k)

        # 3. Navigation Buttons (Row 2)
        btn_back = discord.ui.Button(label="« Back to Overview", style=discord.ButtonStyle.secondary, emoji="↩️", row=2)
        btn_back.callback = self.go_back
        self.add_item(btn_back)

        btn_cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", row=2)
        btn_cancel.callback = self.cancel_search
        self.add_item(btn_cancel)

    async def on_select_release(self, interaction: discord.Interaction):
        select = [x for x in self.children if isinstance(x, discord.ui.Select)][0]
        chosen_idx = int(select.values[0])
        top_releases = self.real_releases.get("top_releases", [])
        if chosen_idx >= len(top_releases):
            await interaction.response.send_message("❌ Release not found.", ephemeral=True)
            return

        chosen_release = top_releases[chosen_idx]
        q_label = chosen_release.get("quality_label", "HD")
        q_key = chosen_release.get("quality_key", "1080p")
        is_4k = (q_key == "4k")
        sz_val = chosen_release.get("size_gb", 0)
        seeds = chosen_release.get("seeders", 0)
        idx_name = chosen_release.get("indexer", "Tracker")
        rel_t = chosen_release.get("title", self.title)

        confirm_embed = discord.Embed(
            title=f"⚠️ Confirm Release Download: {self.title}",
            description=(
                f"### 📦 Selected Release Information:\n"
                f"• **Release Name:** `{rel_t}`\n"
                f"• **🎯 Quality:** `{q_label}`\n"
                f"• **📦 Exact File Size:** `{sz_val:.2f} GB`\n"
                f"• **👥 Tracker Health:** `{seeds} Seeders` via `{idx_name}`\n"
                f"• **📁 Target Library:** `{self.media_type.upper()}`\n\n"
                f"Are you sure you want to download this release to homelab storage?"
            ),
            color=discord.Color.gold(),
        )
        confirm_embed.set_footer(text="Click 'Confirm Download' to queue or 'Back to Releases' to choose another")

        view = SpecificReleaseConfirmationView(
            media_id=self.media_id,
            title=self.title,
            target_type=self.media_type,
            release=chosen_release,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
            releases_embed=self.releases_embed or interaction.message.embeds[0],
            original_embed=self.original_embed,
            is_4k=is_4k,
        )
        await interaction.response.edit_message(embed=confirm_embed, view=view)

    async def download_1080(self, interaction: discord.Interaction):
        await self._transition_confirm(interaction, quality="1080p", is_4k=False)

    async def download_4k(self, interaction: discord.Interaction):
        await self._transition_confirm(interaction, quality="4k", is_4k=True)

    async def _transition_confirm(self, interaction: discord.Interaction, quality: str, is_4k: bool):
        q_label = "4K Ultra-HD" if is_4k else "Standard HD (1080p)"
        avg_sz = self.size_estimates.get(f"{quality}_avg_gb", 4.0 if not is_4k else 12.0)
        max_sz = self.size_estimates.get(f"{quality}_max_gb", 8.0 if not is_4k else 20.0)

        q_real = self.real_releases.get("qualities", {}).get(quality, {})
        real_note = ""
        if q_real:
            real_note = f"\n• **Live Tracker Releases:** `{q_real.get('min_gb')} - {q_real.get('max_gb')} GB` (Avg: `{q_real.get('avg_gb')} GB`, {q_real.get('count')} releases found)"

        confirm_embed = self.original_embed.copy()
        confirm_embed.color = discord.Color.gold()
        confirm_embed.description = (
            f"⚠️ **Confirm Download Size & Quality:**\n"
            f"• **Target Quality:** `{q_label}`\n"
            f"• **Estimated Storage:** ~`{avg_sz:.1f} GB` (Max Cap: `{max_sz:.1f} GB`){real_note}\n\n"
            f"Are you sure you want to download **{self.title}** ({self.media_type.upper()}) in **{q_label}** to homelab storage?"
        )
        view = MediaRequestConfirmationView(
            media_id=self.media_id,
            title=self.title,
            target_type=self.media_type,
            quality=quality,
            embed=confirm_embed,
            is_4k=is_4k,
            initial_embed=self.original_embed,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
        )
        await interaction.response.edit_message(embed=confirm_embed, view=view)

    async def go_back(self, interaction: discord.Interaction):
        view = MediaRequestInitialView(
            media_id=self.media_id,
            title=self.title,
            media_type=self.media_type,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
            embed=self.original_embed,
        )
        await interaction.response.edit_message(embed=self.original_embed, view=view)

    async def cancel_search(self, interaction: discord.Interaction):
        cancel_embed = self.original_embed.copy()
        cancel_embed.color = discord.Color.dark_grey()
        cancel_embed.add_field(
            name="Request Status",
            value="❌ Request cancelled.",
            inline=False,
        )
        await interaction.response.edit_message(embed=cancel_embed, view=None)


class MediaRequestInitialView(discord.ui.View):
    """
    Initial Selection View with dynamically populated buttons matching the exact media type
    (Movie-only for movies, TV-only for TV shows) and interactive tracker release inspection.
    """

    def __init__(
        self,
        media_id: int,
        title: str,
        media_type: str,
        size_estimates: Dict[str, Any],
        real_releases: Dict[str, Any],
        embed: discord.Embed,
        requested_quality: Optional[str] = None,
        requested_season: Optional[int] = None,
        all_results: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__(timeout=180)
        self.media_id = media_id
        self.title = title
        self.media_type = media_type.lower()
        self.size_estimates = size_estimates
        self.real_releases = real_releases or {"has_releases": False}
        self.embed = embed
        self.requested_quality = requested_quality
        self.requested_season = requested_season
        self.all_results = all_results or []

        # 1. Result dropdown selector if multiple search results are found
        if len(self.all_results) > 1:
            options = []
            for r in self.all_results[:5]:
                r_id = str(r.get("id"))
                r_title = r.get("title") or r.get("name") or "Unknown"
                r_type = (r.get("mediaType") or "movie").capitalize()
                r_date = r.get("releaseDate") or r.get("firstAirDate") or ""
                r_year = f" ({r_date[:4]})" if len(r_date) >= 4 else ""
                label = f"{r_title}{r_year}"[:95]
                desc = f"{r_type} • TMDB ID: {r_id}"
                is_default = (r.get("id") == self.media_id)
                options.append(discord.SelectOption(label=label, value=f"{r.get('mediaType', 'movie')}:{r_id}", description=desc, default=is_default))

            select = discord.ui.Select(placeholder="🔍 Pick a different title from results...", options=options, row=0)
            select.callback = self.on_select_different_result
            self.add_item(select)

        # 2. Dynamic Action Buttons based strictly on media_type
        if self.media_type == "movie":
            # For Movies: ONLY show Movie download options
            if self.requested_quality == "4k":
                btn_4k = discord.ui.Button(label="Confirm 4K / UHD Download", style=discord.ButtonStyle.success, emoji="✨", row=1)
                btn_4k.callback = self.request_movie_4k
                self.add_item(btn_4k)

                btn_1080 = discord.ui.Button(label="Request 1080p HD instead", style=discord.ButtonStyle.secondary, emoji="🎬", row=1)
                btn_1080.callback = self.request_movie_1080
                self.add_item(btn_1080)
            else:
                btn_1080 = discord.ui.Button(label="Request Movie (1080p HD)", style=discord.ButtonStyle.primary, emoji="🎬", row=1)
                btn_1080.callback = self.request_movie_1080
                self.add_item(btn_1080)

                btn_4k = discord.ui.Button(label="Request 4K / UHD", style=discord.ButtonStyle.secondary, emoji="✨", row=1)
                btn_4k.callback = self.request_movie_4k
                self.add_item(btn_4k)

        elif self.media_type == "tv":
            # For TV Shows: ONLY show TV series download options
            if self.requested_season:
                btn_s = discord.ui.Button(label=f"Request Season {self.requested_season}", style=discord.ButtonStyle.primary, emoji="📺", row=1)
                btn_s.callback = self.request_tv_specific_season
                self.add_item(btn_s)

                btn_all = discord.ui.Button(label="Request All Seasons (1080p)", style=discord.ButtonStyle.secondary, emoji="📚", row=1)
                btn_all.callback = self.request_tv_all_1080
                self.add_item(btn_all)
            else:
                if self.requested_quality == "4k":
                    btn_4k = discord.ui.Button(label="Confirm 4K TV Series Download", style=discord.ButtonStyle.success, emoji="✨", row=1)
                    btn_4k.callback = self.request_tv_all_4k
                    self.add_item(btn_4k)

                    btn_1080 = discord.ui.Button(label="Request TV Series (1080p)", style=discord.ButtonStyle.secondary, emoji="📺", row=1)
                    btn_1080.callback = self.request_tv_all_1080
                    self.add_item(btn_1080)
                else:
                    btn_1080 = discord.ui.Button(label="Request TV Series (1080p)", style=discord.ButtonStyle.primary, emoji="📺", row=1)
                    btn_1080.callback = self.request_tv_all_1080
                    self.add_item(btn_1080)

                    btn_4k = discord.ui.Button(label="Request TV Series (4K)", style=discord.ButtonStyle.secondary, emoji="✨", row=1)
                    btn_4k.callback = self.request_tv_all_4k
                    self.add_item(btn_4k)

        # 3. Interactive Release Search Viewer Button (ALWAYS VISIBLE)
        btn_releases = discord.ui.Button(label="Interactive Release Search", style=discord.ButtonStyle.secondary, emoji="🔍", row=2)
        btn_releases.callback = self.view_live_releases
        self.add_item(btn_releases)

        # 4. Cancel Button
        btn_cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", row=2)
        btn_cancel.callback = self.cancel_initial
        self.add_item(btn_cancel)

    async def request_movie_1080(self, interaction: discord.Interaction):
        await self._transition_to_confirm(interaction, quality="1080p", is_4k=False)

    async def request_movie_4k(self, interaction: discord.Interaction):
        await self._transition_to_confirm(interaction, quality="4k", is_4k=True)

    async def request_tv_all_1080(self, interaction: discord.Interaction):
        await self._transition_to_confirm(interaction, quality="1080p", is_4k=False, seasons="all")

    async def request_tv_all_4k(self, interaction: discord.Interaction):
        await self._transition_to_confirm(interaction, quality="4k", is_4k=True, seasons="all")

    async def request_tv_specific_season(self, interaction: discord.Interaction):
        s_val = str(self.requested_season) if self.requested_season else "1"
        await self._transition_to_confirm(interaction, quality="1080p", is_4k=False, seasons=s_val)

    async def _transition_to_confirm(self, interaction: discord.Interaction, quality: str, is_4k: bool, seasons: str = "all"):
        q_label = "4K Ultra-HD" if is_4k else "Standard HD (1080p)"
        avg_sz = self.size_estimates.get(f"{quality}_avg_gb", 3.5 if not is_4k else 10.0)
        max_sz = self.size_estimates.get(f"{quality}_max_gb", 7.5 if not is_4k else 18.5)

        q_real = self.real_releases.get("qualities", {}).get(quality, {})
        real_note = ""
        if q_real:
            real_note = f"\n• **Live Tracker Range:** `{q_real.get('min_gb')} - {q_real.get('max_gb')} GB` (Avg: `{q_real.get('avg_gb')} GB`, {q_real.get('count')} releases)"

        season_text = f"\n• **Scope:** `Season {seasons}`" if (self.media_type == "tv" and seasons != "all") else ("\n• **Scope:** `All Seasons`" if self.media_type == "tv" else "")

        confirm_embed = self.embed.copy()
        confirm_embed.color = discord.Color.gold()
        confirm_embed.description = (
            f"⚠️ **Confirm Download Size & Profile:**\n"
            f"• **Target Quality:** `{q_label}`{season_text}\n"
            f"• **Estimated Storage Impact:** ~`{avg_sz:.1f} GB` (Max Cap: `{max_sz:.1f} GB`){real_note}\n\n"
            f"Are you sure you want to download **{self.title}** as **{self.media_type.upper()}** ({q_label})?"
        )
        confirm_view = MediaRequestConfirmationView(
            media_id=self.media_id,
            title=self.title,
            target_type=self.media_type,
            quality=quality,
            embed=confirm_embed,
            is_4k=is_4k,
            seasons=seasons,
            initial_embed=self.embed,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
        )
        await interaction.response.edit_message(embed=confirm_embed, view=confirm_view)

    async def view_live_releases(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # If not cached yet, query Prowlarr on demand
        if not self.real_releases.get("has_releases"):
            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable if os.path.exists(sys.executable) else "python3",
                    CLI_PATH,
                    "--json",
                    "prowlarr",
                    "releases",
                    self.title,
                    "--media-type",
                    self.media_type,
                ],
                capture_output=True,
                text=True,
                timeout=45,
                cwd=WORKSPACE_DIR,
                shell=False,
            )
            if proc.returncode == 0 and proc.stdout:
                try:
                    self.real_releases = json.loads(proc.stdout)
                except Exception:
                    pass

        releases = self.real_releases.get("top_releases", [])
        qualities = self.real_releases.get("qualities", {})

        if not releases:
            await interaction.followup.send(
                f"🔍 No active releases currently indexed on trackers for **{self.title}**. You can still request using standard profiles.",
                ephemeral=True,
            )
            return

        q_lines = []
        for q_name in ["4k", "1080p", "720p", "other"]:
            if q_name in qualities:
                qinfo = qualities[q_name]
                q_disp = "2160p 4K UHD" if q_name == "4k" else ("1080p FHD" if q_name == "1080p" else ("720p HD" if q_name == "720p" else "Other"))
                q_lines.append(f"• **{q_disp}:** `{qinfo['min_gb']:.2f} - {qinfo['max_gb']:.2f} GB` (Avg: `{qinfo['avg_gb']:.1f} GB`, `{qinfo['count']}` releases)")

        q_summary_str = "\n".join(q_lines) if q_lines else "• Various release formats found"

        lines = []
        for idx, r in enumerate(releases[:8], 1):
            t = r.get("title", "Unknown")
            sz = r.get("size_gb", 0)
            seeds = r.get("seeders", 0)
            indexer = r.get("indexer", "Tracker")
            q_label = r.get("quality_label", "HD")
            lines.append(f"**{idx}. `{t}`**\n🎯 **Quality:** `{q_label}` • 📦 **Size:** `{sz:.2f} GB` • 👥 **Seeds:** `{seeds}` • 🏷️ `{indexer}`")

        rel_embed = discord.Embed(
            title=f"📡 Interactive Tracker Search: {self.title}",
            description=(
                f"### 📊 Live Tracker Quality Breakdown:\n{q_summary_str}\n\n"
                f"### ⚡ Top Seeded Releases on Trackers:\n" + "\n\n".join(lines)
            ),
            color=discord.Color.teal(),
        )
        rel_embed.set_footer(text="Select a release from the dropdown or click a profile button below")

        view = LiveReleasesView(
            media_id=self.media_id,
            title=self.title,
            media_type=self.media_type,
            size_estimates=self.size_estimates,
            real_releases=self.real_releases,
            original_embed=self.embed,
            releases_embed=rel_embed,
        )
        await interaction.edit_original_response(embed=rel_embed, view=view)

    async def cancel_initial(self, interaction: discord.Interaction):
        self.embed.color = discord.Color.dark_grey()
        self.embed.add_field(
            name="Request Status",
            value="❌ Request cancelled.",
            inline=False,
        )
        await interaction.response.edit_message(embed=self.embed, view=None)

    async def on_select_different_result(self, interaction: discord.Interaction):
        select = [x for x in self.children if isinstance(x, discord.ui.Select)][0]
        chosen_val = select.values[0]
        chosen_type, chosen_id_str = chosen_val.split(":", 1)
        chosen_id = int(chosen_id_str)

        await interaction.response.defer()

        # Fetch details for the newly chosen item
        detail_proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "jellyseerr",
                "details",
                "--media-type",
                chosen_type,
                "--id",
                str(chosen_id),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        detail_json = {}
        if detail_proc.returncode == 0 and detail_proc.stdout:
            try:
                detail_json = json.loads(detail_proc.stdout)
            except Exception:
                pass

        new_title = detail_json.get("title") or detail_json.get("name") or "Unknown"
        rel_date = detail_json.get("releaseDate") or detail_json.get("firstAirDate") or ""
        year_str = f" ({rel_date[:4]})" if len(rel_date) >= 4 else ""
        new_overview = detail_json.get("overview") or "No overview available."
        if len(new_overview) > 350:
            new_overview = new_overview[:347] + "..."

        poster_path = detail_json.get("posterPath")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

        size_estimates = detail_json.get("size_estimates", {})
        real_releases = detail_json.get("real_releases", {})

        new_embed = build_media_embed(
            item_title=new_title,
            year=year_str,
            overview=new_overview,
            media_type=chosen_type,
            tmdb_id=chosen_id,
            poster_url=poster_url,
            size_estimates=size_estimates,
            real_releases=real_releases,
            detail_json=detail_json,
        )

        new_view = MediaRequestInitialView(
            media_id=chosen_id,
            title=new_title,
            media_type=chosen_type,
            size_estimates=size_estimates,
            real_releases=real_releases,
            embed=new_embed,
            requested_quality=self.requested_quality,
            requested_season=self.requested_season,
            all_results=self.all_results,
        )
        await interaction.edit_original_response(embed=new_embed, view=new_view)


# --- Custom Bot Class ---

class HomelabBot(commands.Bot):
    """Subclassed bot to ensure reliable startup hooks, guild fast-sync, events, ntfy listener, and presence."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.ntfy_task: Optional[asyncio.Task] = None
        self.remediation_task: Optional[asyncio.Task] = None
        self._synced = False

    async def setup_hook(self):
        # Register commands to configured Guild ID for instant zero-delay propagation
        if GUILD_ID and not self._synced:
            try:
                guild_obj = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild_obj)
                synced_guild = await self.tree.sync(guild=guild_obj)
                print(f"⚡ Direct guild fast-sync complete ({len(synced_guild)} command(s) for guild {GUILD_ID}).", flush=True)
                self._synced = True
            except Exception as e:
                print(f"Direct guild sync warning: {e}", flush=True)

        if not self._synced:
            try:
                synced_global = await self.tree.sync()
                print(f"Global slash command tree synced: {len(synced_global)} command(s).", flush=True)
                self._synced = True
            except Exception as e:
                print(f"Global slash command sync warning: {e}", flush=True)

    async def on_ready(self):
        print(f"🤖 Gateway online. Logged in as: {self.user.name} ({self.user.id})", flush=True)
        await self.change_presence(activity=discord.Game(name="Orchestrating Homelab SRE"))

        # Launch background NTFY stream listener
        if not self.ntfy_task or self.ntfy_task.done():
            self.ntfy_task = asyncio.create_task(self.ntfy_event_listener_loop())

        # Launch autonomous stalled download remediation watchdog loop
        if not self.remediation_task or self.remediation_task.done():
            self.remediation_task = asyncio.create_task(self.queue_auto_remediation_loop())

    async def on_member_join(self, member: discord.Member):
        print(f"👋 New member joined: {member.name} ({member.id}) in guild {member.guild.name}", flush=True)
        channel = None
        try:
            channel = await create_user_request_channel(member.guild, member)
        except discord.errors.Forbidden:
            print(f"❌ Permission error auto-provisioning channel for {member.name}: Bot lacks 'Administrator' or category permissions.", flush=True)
        except Exception as e:
            print(f"❌ Error auto-provisioning channel for joining member {member.name}: {e}", flush=True)

        # Send a direct welcome DM to the new member
        try:
            dm_desc = (
                f"Welcome to **{member.guild.name}**, {member.name}! 🎬\n\n"
                + (f"Your personal private media request channel is ready: <#{channel.id}>\n\n" if channel else "You can create your personal request space anytime by running **`/open-channel`** in the server!\n\n")
                + "### 🚀 Quick Start Commands:\n"
                + "• **`/request <title>`** — Search TMDB, view download size estimates, and submit movie/TV requests.\n"
                + "• **`/status`** — View the live download queue, progress bars, and request status.\n"
                + "• **`/open-channel`** — Retrieve a direct link to your private request channel."
            )
            dm_embed = discord.Embed(
                title="🍿 Welcome to Homelab Media Requests!",
                description=dm_desc,
                color=discord.Color.blue(),
            )
            dm_embed.set_footer(text=f"{BOT_BRAND} • Media Gateway")
            await member.send(embed=dm_embed)
        except Exception:
            # DMs might be blocked by user privacy settings
            pass

    async def ntfy_event_listener_loop(self):
        """Streams real-time events from ntfy and posts them to the ops channel."""
        print(f"📡 Initializing real-time NTFY alert listener on {NTFY_STREAM_URL}...", flush=True)

        # Seed existing event IDs so we do not spam old events on boot
        try:
            def fetch_seed():
                req = urllib.request.Request(f"{NTFY_STREAM_URL}?poll=1", headers={"User-Agent": "HomelabBot/1.0"})
                with urllib.request.urlopen(req, timeout=10) as sr:
                    return sr.read().decode(errors="ignore")

            raw_seed = await asyncio.to_thread(fetch_seed)
            for sline in raw_seed.split("\n"):
                s_str = sline.strip()
                if s_str:
                    try:
                        s_data = json.loads(s_str)
                        if s_data.get("id"):
                            seen_ntfy_ids.add(s_data["id"])
                    except Exception:
                        pass
            print(f"📡 NTFY listener seeded with {len(seen_ntfy_ids)} prior event ID(s). Live listener active.", flush=True)
        except Exception as seed_err:
            print(f"Warning: NTFY seed fetch: {seed_err}", flush=True)

        while not self.is_closed():
            try:
                def fetch_poll():
                    req = urllib.request.Request(f"{NTFY_STREAM_URL}?poll=1", headers={"User-Agent": "HomelabBot/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as pr:
                        return pr.read().decode(errors="ignore")

                raw_poll = await asyncio.to_thread(fetch_poll)
                for line in raw_poll.split("\n"):
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        evt = json.loads(line_str)
                    except Exception:
                        continue

                    if evt.get("event") != "message":
                        continue

                    evt_id = evt.get("id")
                    if not evt_id or evt_id in seen_ntfy_ids:
                        continue

                    seen_ntfy_ids.add(evt_id)
                    if len(seen_ntfy_ids) > 300:
                        seen_ntfy_ids.pop()

                    title = evt.get("title") or "Jellyseerr Media Notification"
                    message = evt.get("message") or ""
                    attachment = evt.get("attachment") or {}
                    img_url = attachment.get("url") if isinstance(attachment, dict) else None

                    ops_ch = self.get_channel(OPS_CHANNEL_ID) if OPS_CHANNEL_ID else None
                    if ops_ch:
                        tag_line = f"🔔 {operator_mention()} **New Intake Alert!**" if OPERATOR_ID else "🔔 **New Intake Alert!**"
                        alert_embed = discord.Embed(
                            title=f"🎬 {title}",
                            description=message,
                            color=discord.Color.gold(),
                        )
                        if img_url:
                            alert_embed.set_thumbnail(url=img_url)
                        alert_embed.set_footer(text="Homelab Automated Intake Alert • ntfy stream")
                        await ops_ch.send(content=tag_line, embed=alert_embed)
                        print(f"Broadcast ntfy alert to the ops channel: {title}", flush=True)

            except Exception as e:
                pass

            await asyncio.sleep(6)

    async def queue_auto_remediation_loop(self):
        """Periodically checks Radarr & Sonarr queues for stalled downloads, blocklists them, and triggers re-search."""
        await asyncio.sleep(45)  # Initial grace period on startup
        while not self.is_closed():
            try:
                def run_remediation():
                    return subprocess.run(
                        [sys.executable if os.path.exists(sys.executable) else "python3", CLI_PATH, "--json", "queue", "auto-remediate"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=WORKSPACE_DIR,
                    )

                res = await asyncio.to_thread(run_remediation)
                if res.returncode == 0 and res.stdout:
                    try:
                        data = json.loads(res.stdout)
                        remediated = data.get("remediated", [])
                        if remediated:
                            if OPS_CHANNEL_ID:
                                ops_channel = self.get_channel(OPS_CHANNEL_ID)
                                if ops_channel:
                                    for item in remediated:
                                        embed = discord.Embed(
                                            title="⚡ Autonomous Stalled Download Auto-Remediated",
                                            description=(
                                                f"**Title:** {item.get('title')}\n"
                                                f"**Type:** {item.get('type', '').upper()}\n"
                                                f"**Diagnosis:** `{item.get('reason')}`\n\n"
                                                f"🛠️ **Action Taken:** Removed stalled release from download client, blocklisted bad release hash, and triggered immediate search for a healthy alternative release."
                                            ),
                                            color=discord.Color.gold(),
                                            timestamp=datetime.now(),
                                        )
                                        embed.set_footer(text=f"{BOT_BRAND} • Autonomous Watchdog")
                                        await ops_channel.send(embed=embed)
                                        print(f"⚡ Auto-remediated stalled download: {item.get('title')}", flush=True)

                            # Send clean, friendly non-technical updates to media request channels
                            if REQUEST_CATEGORY_ID and self.guilds:
                                operator = operator_mention()
                                for g in self.guilds:
                                    cat = g.get_channel(REQUEST_CATEGORY_ID)
                                    if cat and isinstance(cat, discord.CategoryChannel):
                                        for item in remediated:
                                            user_embed = discord.Embed(
                                                title=f"⏳ Quick Update: {item.get('title')}",
                                                description=(
                                                    f"The initial download for **{item.get('title')}** was moving slowly or stalled.\n\n"
                                                    "✨ Automatically switched to a fresh, faster source! It's on its way, but if anything seems stuck feel free to ping "
                                                    f"{operator}, who will look into it."
                                                ),
                                                color=discord.Color.blue(),
                                            )
                                            user_embed.set_footer(text="Homelab Automated Media Gateway")
                                            for text_ch in cat.text_channels:
                                                if text_ch.name.startswith("req-"):
                                                    try:
                                                        await text_ch.send(embed=user_embed)
                                                    except Exception:
                                                        pass
                    except Exception:
                        pass
            except Exception:
                pass

            await asyncio.sleep(300)  # Check every 5 minutes


bot = HomelabBot()


# --- Household Plane Slash Commands (/request, /status, /open-channel) ---

@bot.tree.command(name="open-channel", description="Provision or access your personal private media request channel")
async def slash_open_channel(interaction: discord.Interaction):
    """Allows existing members to provision their personal private media request channel on demand."""
    if not interaction.guild:
        await interaction.response.send_message("❌ This command can only be used inside the homelab Discord server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        channel = await create_user_request_channel(interaction.guild, interaction.user)
        if channel:
            await interaction.followup.send(
                f"✅ Your private media request channel is ready: <#{channel.id}>\nHead over to search, request content, and check download status!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("❌ Failed to provision private channel. Please contact an administrator.", ephemeral=True)
    except discord.errors.Forbidden:
        await interaction.followup.send(
            "❌ **Missing Permissions / Access:** The bot cannot view or manage channels in the **📽️ Media Requests** category.\n"
            "👉 Grant the bot's role **Administrator** in **Server Settings -> Roles**, or add it to the **📽️ Media Requests** category permissions with **View Channel** and **Manage Channels** enabled.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error provisioning channel: `{str(e)}`", ephemeral=True)


@bot.tree.command(name="request", description="Search and request movies or TV series in Jellyseerr")
@app_commands.describe(title="Title to search for. Quality and season hints are understood, e.g. 'example movie 4k' or 'example series season 2'")
async def slash_request(interaction: discord.Interaction, title: str):
    """Slash command to search TMDB via Jellyseerr, estimate size, and confirm download."""
    # Channel Guardrail: Check if invoked in authorized request category or DM
    if not is_request_channel_permitted(interaction.channel):
        await interaction.response.send_message(
            "⚠️ Media requests should be submitted in your private channel under the **📽️ Media Requests** category or in DMs! Run `/open-channel` to get your channel link.",
            ephemeral=True,
        )
        return

    # Sanitize user input
    sanitized_title = sanitize_input(title)
    if not sanitized_title:
        await interaction.response.send_message("❌ Please provide a valid title to search for.", ephemeral=True)
        return

    # Ephemeral deferral to isolate search and views to the invoking user
    await interaction.response.defer(ephemeral=True)

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "jellyseerr",
                "search",
                sanitized_title,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        if proc.returncode != 0:
            err_detail = proc.stderr or proc.stdout or f"CLI returned code {proc.returncode}"
            await interaction.followup.send(f"❌ Error searching Jellyseerr: `{err_detail.strip()}`", ephemeral=True)
            return

        try:
            res = json.loads(proc.stdout) if proc.stdout else {}
        except json.JSONDecodeError as json_err:
            await interaction.followup.send(f"❌ Error parsing Jellyseerr response: `{json_err}`", ephemeral=True)
            return

        if isinstance(res, dict) and res.get("error"):
            err_msg = res.get("message") or res.get("response") or "Unknown error"
            await interaction.followup.send(f"❌ Jellyseerr Search Error: `{err_msg}`", ephemeral=True)
            return

        results = res.get("results", []) if isinstance(res, dict) else []
        parsed_query = res.get("parsed_query", {}) if isinstance(res, dict) else {}
        clean_title = parsed_query.get("clean_title", sanitized_title)
        requested_quality = parsed_query.get("quality")
        requested_season = parsed_query.get("season")

        if not results:
            hint = f" (searched as `{clean_title}`)" if clean_title != sanitized_title else ""
            await interaction.followup.send(f"🔍 No results found for `{sanitized_title}`{hint} in Jellyseerr.", ephemeral=True)
            return

        top = results[0]
        top_id = top.get("id")
        media_type = top.get("mediaType", "movie")
        item_title = top.get("title") or top.get("name") or clean_title
        rel_date = top.get("releaseDate") or top.get("firstAirDate") or ""
        year = f" ({rel_date[:4]})" if len(rel_date) >= 4 else ""
        overview = top.get("overview") or "No overview available."
        if len(overview) > 350:
            overview = overview[:347] + "..."

        poster_path = top.get("posterPath")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

        # Fetch detailed metadata and real releases
        detail_proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "jellyseerr",
                "details",
                "--media-type",
                media_type,
                "--id",
                str(top_id),
            ],
            capture_output=True,
            text=True,
            timeout=25,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        detail_json = {}
        if detail_proc.returncode == 0 and detail_proc.stdout:
            try:
                detail_json = json.loads(detail_proc.stdout)
            except Exception:
                pass

        size_estimates = detail_json.get("size_estimates", {
            "1080p_avg_gb": 3.5, "1080p_max_gb": 7.5,
            "4k_avg_gb": 10.0, "4k_max_gb": 18.5
        })
        real_releases = detail_json.get("real_releases", {"has_releases": False})

        embed = build_media_embed(
            item_title=item_title,
            year=year,
            overview=overview,
            media_type=media_type,
            tmdb_id=top_id,
            poster_url=poster_url,
            size_estimates=size_estimates,
            real_releases=real_releases,
            detail_json=detail_json,
        )

        view = MediaRequestInitialView(
            media_id=top_id,
            title=item_title,
            media_type=media_type,
            size_estimates=size_estimates,
            real_releases=real_releases,
            embed=embed,
            requested_quality=requested_quality,
            requested_season=requested_season,
            all_results=results,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    except asyncio.TimeoutError:
        await interaction.followup.send(f"⚠️ Search for `{sanitized_title}` timed out.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error searching for `{sanitized_title}`: `{str(e)}`", ephemeral=True)


# --- Status Command & Pipeline Diagnostics UI ---

def build_status_overview_embed(status_data: Dict[str, Any]) -> discord.Embed:
    downloads = status_data.get("active_downloads", [])
    requests = status_data.get("in_progress_requests") or status_data.get("recent_requests", [])

    embed = discord.Embed(
        title="📡 Homelab Media & Download Status",
        color=discord.Color.blue(),
    )

    # Active Downloads Field
    if downloads:
        dl_lines = []
        for d in downloads[:6]:
            icon = "🎬" if d.get("type") == "movie" else "📺"
            title = d.get("title", "Unknown")
            pbar = d.get("progress_bar", "░░░░░░░░░░")
            pct = d.get("progress_pct", 0.0)
            human_eta = format_human_eta(d.get("timeleft"))
            sz_left = d.get("sizeleft_str", "")
            sz_total = d.get("size_str", "")
            sz_note = f" • `{sz_left} left / {sz_total}`" if (sz_left and sz_total) else ""
            dl_lines.append(f"{icon} **{title}**\n`{pbar}` **{pct:.1f}%** • {human_eta}{sz_note}")
        embed.add_field(
            name=f"⚡ Active Downloads ({len(downloads)})",
            value="\n\n".join(dl_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="⚡ Active Downloads",
            value="*No active downloads in queue.*",
            inline=False,
        )

    # In-Progress Requests Field
    if requests:
        req_lines = []
        for r in requests[:10]:
            badge = r.get("badge", "⏳")
            title = r.get("title", "Unknown")
            year = r.get("year", "")
            mtype = r.get("type", "movie").capitalize()
            season_str = r.get("season_str")
            season_part = f", {season_str}" if season_str else ""
            st = r.get("status_label", "In Progress")
            by = r.get("requested_by", "User")
            req_lines.append(f"{badge} **{title}{year}** ({mtype}{season_part}) — `{st}` *(by {by})*")
        embed.add_field(
            name=f"⏳ In-Progress Requests ({len(requests)})",
            value="\n".join(req_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="⏳ In-Progress Requests",
            value="*No in-progress requests found. All requested media is up to date!*",
            inline=False,
        )

    if downloads or requests:
        embed.set_footer(text="Homelab SRE Bridge • Click 'See Details' below to inspect live pipeline status")
    else:
        embed.set_footer(text=f"{BOT_BRAND} • SRE Bridge")

    return embed


def build_pipeline_details_embed(data: Dict[str, Any]) -> discord.Embed:
    if not data or data.get("error"):
        err_msg = data.get("message", "Unable to load pipeline diagnostics for this item.") if isinstance(data, dict) else "No data returned from backend."
        err_embed = discord.Embed(
            title="❌ Pipeline Lookup Error",
            description=err_msg,
            color=discord.Color.red(),
        )
        return err_embed

    title_str = f"{data.get('title', 'Unknown')}{data.get('year', '')}"
    mtype = str(data.get("media_type", "movie")).upper()
    stage_num = data.get("stage_num", 1)

    # Stage Color
    if stage_num == 5:
        color = discord.Color.green()
    elif stage_num in (3, 4):
        color = discord.Color.blue()
    elif stage_num == 2:
        color = discord.Color.purple()
    else:
        color = discord.Color.gold()

    embed = discord.Embed(
        title=f"📡 Pipeline Details: {title_str}",
        color=color,
    )

    overview = data.get("overview", "")
    if len(overview) > 220:
        overview = overview[:217] + "..."
    overview_text = f"*{overview}*\n\n" if (overview and overview != "No overview available.") else ""

    stage_name = data.get("stage_name", "Processing")
    stage_desc = data.get("stage_desc", "")
    tracker = data.get("tracker", "")
    pbar = data.get("progress_bar", "░░░░░░░░░░")
    pct = data.get("progress_pct", 0.0)
    progress_ctx = data.get("progress_context", "")

    embed.description = (
        f"{overview_text}"
        f"📍 **Current Location in Pipeline:**\n"
        f"### **{stage_name}**\n"
        f"{stage_desc}\n\n"
        f"🗺️ **Pipeline Flow:**\n"
        f"`{tracker}`\n\n"
        f"📊 **Pipeline Progress:**\n"
        f"`{pbar}` **{pct:.1f}%**\n"
        f"↳ *{progress_ctx}*"
    )

    # 1. Jellyseerr Intake Field
    created_at_raw = data.get("created_at")
    date_display = "Recently"
    if created_at_raw:
        try:
            dt_clean = created_at_raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(dt_clean)
            ts = int(dt.timestamp())
            date_display = f"<t:{ts}:R>"
        except Exception:
            date_display = str(created_at_raw)[:10]

    j_lines = [
        f"• **Request ID:** `#{data.get('id', 'N/A')}`",
        f"• **Requester:** `{data.get('requested_by', 'User')}`",
        f"• **Profile:** `{data.get('profile_name', 'HD-1080p')}`",
    ]
    if data.get("season_str"):
        j_lines.append(f"• **Scope:** `{data.get('season_str')}`")
    j_lines.append(f"• **Submitted:** {date_display}")

    embed.add_field(
        name="🟡 Jellyseerr Intake",
        value="\n".join(j_lines),
        inline=True,
    )

    # 2. Arr Stack Field
    arr_svc = data.get("arr_service", "Arr Stack")
    arr_id_val = data.get("arr_id")
    arr_data = data.get("arr_data") or {}
    monitored_str = "Yes" if arr_data.get("monitored") else ("No" if arr_data else "Pending")

    arr_lines = [
        f"• **Service:** `{arr_svc}` (ID: `#{arr_id_val or 'N/A'}`)",
        f"• **Monitored:** `{monitored_str}`",
    ]
    if mtype == "MOVIE":
        has_file = arr_data.get("hasFile")
        sz_disk = human_size(arr_data.get("sizeOnDisk", 0))
        arr_lines.append(f"• **File on Disk:** `{'Yes (' + sz_disk + ')' if has_file else 'No'}`")
    else:
        stats = arr_data.get("statistics") or {}
        f_count = stats.get("episodeFileCount", 0)
        e_count = stats.get("episodeCount", 0)
        sz_disk = human_size(stats.get("sizeOnDisk", 0))
        arr_lines.append(f"• **Episodes on Disk:** `{f_count}/{e_count} ({sz_disk})`")

    if data.get("library_path"):
        short_path = data["library_path"]
        if len(short_path) > 35:
            short_path = "..." + short_path[-32:]
        arr_lines.append(f"• **Path:** `{short_path}`")

    embed.add_field(
        name=f"🟣 {arr_svc} Stack",
        value="\n".join(arr_lines),
        inline=True,
    )

    # 3. Downloader & Network Download Field (if active queue item)
    u_info = data.get("useful_info") or {}
    if data.get("queue_item") or u_info.get("download_title"):
        human_eta = format_human_eta(u_info.get("timeleft"))
        qb_lines = [
            f"• **Download Progress:** `{u_info.get('size_left', '0 B')} remaining` • **{human_eta}**",
            f"• **Download Speed:** `{u_info.get('dlspeed', '0 B/s')} ↓`",
        ]
        embed.add_field(
            name="📥 Live Download Status",
            value="\n".join(qb_lines),
            inline=False,
        )

    # 4. Jellyfin Availability Field
    jf_item = data.get("jf_item")
    media_url = data.get("media_url")
    if jf_item or stage_num == 5:
        jf_text = "✅ **Indexed in Jellyfin (Ready to Stream)**"
        if media_url:
            jf_text += f"\n• [🔗 Click to Open in Jellyfin Web Client]({media_url})"
    elif stage_num == 2 and data.get("media_type") == "tv" and (data.get("arr_data", {}).get("statistics", {}).get("episodeFileCount", 0) > 0):
        f_count = (data.get("arr_data", {}).get("statistics") or {}).get("episodeFileCount", 0)
        jf_text = f"✅ **Partially Available ({f_count} episodes ready in Jellyfin)**"
        if media_url:
            jf_text += f"\n• [🔗 Click to Open in Jellyfin Web Client]({media_url})"
    else:
        jf_text = "⏳ **Pending Download & Library Scan**"

    embed.add_field(
        name="🟢 Jellyfin Media Server",
        value=jf_text,
        inline=False,
    )

    # 5. Diagnostics Notes
    if data.get("notes"):
        notes_lines = [f"• {n}" for n in data["notes"]]
        embed.add_field(
            name="💡 Diagnostics & Next Steps",
            value="\n".join(notes_lines),
            inline=False,
        )

    if data.get("poster_url"):
        embed.set_thumbnail(url=data["poster_url"])

    embed.set_footer(text=f"{BOT_BRAND} • Pipeline Diagnostics")
    return embed


class StatusOverviewView(discord.ui.View):
    """Interactive view for /status overview with 'See Details' button and Refresh."""

    def __init__(self, status_data: Dict[str, Any]):
        super().__init__(timeout=300)
        self.status_data = status_data
        downloads = status_data.get("active_downloads", [])
        requests = status_data.get("in_progress_requests") or status_data.get("recent_requests", [])

        has_items = bool(downloads or requests)

        if has_items:
            btn_details = discord.ui.Button(
                label="See Details",
                style=discord.ButtonStyle.primary,
                emoji="🔍",
                row=0,
            )
            btn_details.callback = self.on_see_details
            self.add_item(btn_details)

        btn_refresh = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            row=0,
        )
        btn_refresh.callback = self.on_refresh
        self.add_item(btn_refresh)

    async def on_see_details(self, interaction: discord.Interaction):
        await interaction.response.defer()

        downloads = self.status_data.get("active_downloads", [])
        requests = self.status_data.get("in_progress_requests") or self.status_data.get("recent_requests", [])

        # Build options for dropdown
        options = []
        default_val = None

        for r in requests[:20]:
            r_id = str(r.get("id"))
            val = f"req:{r_id}"
            if default_val is None:
                default_val = val
            t = r.get("title", "Unknown")
            y = r.get("year", "")
            mtype = r.get("type", "movie").capitalize()
            season_str = r.get("season_str")
            season_note = f" ({season_str})" if season_str else ""
            st = r.get("status_label", "In Progress")
            by = r.get("requested_by", "User")
            label = f"{t}{y}{season_note}"[:95]
            desc = f"{mtype} • {st} • by {by}"[:95]
            emoji = "🎬" if r.get("type") == "movie" else "📺"
            options.append(discord.SelectOption(label=label, value=val, description=desc, emoji=emoji))

        # Also add any standalone active downloads not already in requests
        for d in downloads[:5]:
            d_type = d.get("type", "movie")
            d_id = d.get("movie_id") if d_type == "movie" else d.get("series_id")
            val = f"{d_type}:{d_id}" if d_id else f"queue:{d.get('id')}"
            if any(opt.value == val for opt in options):
                continue
            if default_val is None:
                default_val = val
            t = d.get("title", "Unknown")
            pct = d.get("progress_pct", 0.0)
            eta = d.get("timeleft", "N/A")
            label = f"{t}"[:95]
            desc = f"Downloading ({pct:.1f}%) • ETA: {eta}"[:95]
            emoji = "⚡"
            options.append(discord.SelectOption(label=label, value=val, description=desc, emoji=emoji))

        if not options:
            await interaction.followup.send("❌ No in-progress items found to inspect.", ephemeral=True)
            return

        # Fetch pipeline details for default_val
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "pipeline-details",
                default_val,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        pipeline_json = {}
        if proc.returncode == 0 and proc.stdout:
            try:
                pipeline_json = json.loads(proc.stdout)
            except Exception:
                pass

        embed = build_pipeline_details_embed(pipeline_json)
        details_view = StatusDetailsView(
            options=options,
            current_val=default_val,
            overview_data=self.status_data,
        )
        await interaction.edit_original_response(embed=embed, view=details_view)

    async def on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "status",
                "--take",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        if proc.returncode != 0:
            err_msg = proc.stderr or proc.stdout or "CLI query failed"
            await interaction.followup.send(f"❌ Error refreshing status: `{err_msg.strip()}`", ephemeral=True)
            return

        try:
            status_data = json.loads(proc.stdout) if proc.stdout else {}
        except json.JSONDecodeError as json_err:
            await interaction.followup.send(f"❌ Error parsing response: `{json_err}`", ephemeral=True)
            return

        self.status_data = status_data
        embed = build_status_overview_embed(status_data)
        new_view = StatusOverviewView(status_data=status_data)
        await interaction.edit_original_response(embed=embed, view=new_view)


class StatusDetailsView(discord.ui.View):
    """Interactive view for inspecting item-level pipeline diagnostics with dropdown switcher."""

    def __init__(
        self,
        options: List[discord.SelectOption],
        current_val: str,
        overview_data: Dict[str, Any],
    ):
        super().__init__(timeout=300)
        self.raw_options = options
        self.current_val = current_val
        self.overview_data = overview_data

        # 1. Dropdown Selector on Row 0
        if len(self.raw_options) > 1:
            select_opts = []
            for opt in self.raw_options:
                select_opts.append(
                    discord.SelectOption(
                        label=opt.label,
                        value=opt.value,
                        description=opt.description,
                        emoji=opt.emoji,
                        default=(opt.value == self.current_val),
                    )
                )
            select = discord.ui.Select(
                placeholder="🎯 Select an in-progress request to view details...",
                options=select_opts,
                row=0,
            )
            select.callback = self.on_select_item
            self.add_item(select)

        # 2. Action Buttons on Row 1
        btn_refresh = discord.ui.Button(
            label="Refresh Details",
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            row=1,
        )
        btn_refresh.callback = self.on_refresh_details
        self.add_item(btn_refresh)

        btn_back = discord.ui.Button(
            label="« Back to Status",
            style=discord.ButtonStyle.secondary,
            emoji="◀️",
            row=1,
        )
        btn_back.callback = self.on_back_to_status
        self.add_item(btn_back)

    async def _load_and_display(self, interaction: discord.Interaction, target_val: str):
        self.current_val = target_val
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "pipeline-details",
                target_val,
            ],
            capture_output=True,
            text=True,
            timeout=25,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        pipeline_json = {}
        if proc.returncode == 0 and proc.stdout:
            try:
                pipeline_json = json.loads(proc.stdout)
            except Exception:
                pass

        embed = build_pipeline_details_embed(pipeline_json)

        # Reconstruct view with updated default selection
        new_view = StatusDetailsView(
            options=self.raw_options,
            current_val=self.current_val,
            overview_data=self.overview_data,
        )
        await interaction.edit_original_response(embed=embed, view=new_view)

    async def on_select_item(self, interaction: discord.Interaction):
        await interaction.response.defer()
        select = [x for x in self.children if isinstance(x, discord.ui.Select)][0]
        chosen_val = select.values[0]
        await self._load_and_display(interaction, chosen_val)

    async def on_refresh_details(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self._load_and_display(interaction, self.current_val)

    async def on_back_to_status(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Re-fetch latest status overview
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "status",
                "--take",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        status_data = self.overview_data
        if proc.returncode == 0 and proc.stdout:
            try:
                status_data = json.loads(proc.stdout)
            except Exception:
                pass

        embed = build_status_overview_embed(status_data)
        view = StatusOverviewView(status_data=status_data)
        await interaction.edit_original_response(embed=embed, view=view)


@bot.tree.command(name="status", description="Check active downloads, queue progress, and in-progress media requests")
async def slash_status(interaction: discord.Interaction):
    """Slash command to query Arr download queues and in-progress Jellyseerr requests with interactive pipeline details."""
    # Channel Guardrail: Check if invoked in authorized request category or DM
    if not is_request_channel_permitted(interaction.channel):
        await interaction.response.send_message(
            "⚠️ Status checks should be run inside your private request channel under the **📽️ Media Requests** category or in DMs! Run `/open-channel` to get your channel link.",
            ephemeral=True,
        )
        return

    # Ephemeral deferral to keep status check private
    await interaction.response.defer(ephemeral=True)

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable if os.path.exists(sys.executable) else "python3",
                CLI_PATH,
                "--json",
                "status",
                "--take",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE_DIR,
            shell=False,
        )

        if proc.returncode != 0:
            err_msg = proc.stderr or proc.stdout or "CLI failed"
            await interaction.followup.send(f"❌ Error querying homelab status: `{err_msg.strip()}`", ephemeral=True)
            return

        try:
            status_data = json.loads(proc.stdout) if proc.stdout else {}
        except json.JSONDecodeError as json_err:
            await interaction.followup.send(f"❌ Error decoding status response: `{json_err}`", ephemeral=True)
            return

        embed = build_status_overview_embed(status_data)
        view = StatusOverviewView(status_data=status_data)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    except asyncio.TimeoutError:
        await interaction.followup.send("⚠️ Homelab status query timed out.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error querying homelab status: `{str(e)}`", ephemeral=True)


# --- Operator SRE Plane Execution Engine ---

sre_lock = asyncio.Lock()
sre_queue_count = 0


async def handle_agent_execution(
    channel: Any,
    author: Union[discord.User, discord.Member],
    prompt: str,
    send_response_func,
    reply_status_func=None,
):
    """Core handler for executing SRE agent CLI tasks with safety, FIFO queuing, TTL, and chunking."""
    global sre_queue_count

    category_id = getattr(channel, "category_id", 0)

    # Block SRE commands in all channels under the request category
    if REQUEST_CATEGORY_ID and category_id == REQUEST_CATEGORY_ID:
        ops_hint = f"<#{OPS_CHANNEL_ID}>" if OPS_CHANNEL_ID else "your ops channel"
        await send_response_func(
            f"⛔ SRE Agent commands are not permitted in personal media request channels. Please use {ops_hint} for SRE operations."
        )
        return

    if not is_admin_user(author):
        await send_response_func("⛔ Permission Denied: You do not have the required Homelab Admin role to execute SRE commands.")
        return

    # Check for explicit session reset
    prompt_clean = prompt.strip().lower()
    if prompt_clean in ["reset", "new", "!reset", "!new"]:
        evict_session(channel)
        await send_response_func("🔄 Agent conversation session reset. Next command will run with fresh state.")
        return

    is_queued = sre_lock.locked()
    if is_queued:
        sre_queue_count += 1
        pos = sre_queue_count
        if reply_status_func:
            await reply_status_func(f"⏳ SRE Agent is currently busy executing a task. Your request is queued at **#{pos}**. Waiting for worker slot...")

    async with sre_lock:
        if is_queued:
            sre_queue_count = max(0, sre_queue_count - 1)

        if reply_status_func:
            await reply_status_func("💬 *Thinking... (invoking the SRE agent)*")

        # Keep typing in the channel during execution
        async def keep_typing():
            try:
                while True:
                    await channel.typing()
                    await asyncio.sleep(8)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        typing_task = asyncio.create_task(keep_typing())

        sync_git_repo()

        conversational_prompt = (
            "Respond conversationally in natural language as a grounded engineering peer on Discord. "
            f"Keep it concise, direct, and helpful. User query: {prompt}"
        )

        # The agent CLI is pluggable: any non-interactive coding-agent binary
        # that accepts a prompt as its final argument works here. Configure it
        # with AGENT_CLI_BIN / AGENT_CLI_ARGS rather than editing this file.
        carry_context = should_carry_context(channel)
        command_sequence = [AGENT_CLI_BIN, *AGENT_CLI_ARGS]
        if carry_context and AGENT_CLI_CONTINUE_FLAG:
            command_sequence.append(AGENT_CLI_CONTINUE_FLAG)
        command_sequence.append(conversational_prompt)

        def run_cli_process():
            return subprocess.run(
                command_sequence,
                capture_output=True,
                text=True,
                timeout=AGENT_TIMEOUT_SECONDS,
                cwd=WORKSPACE_DIR,
                shell=False,
            )

        try:
            loop = asyncio.get_event_loop()
            process_execution = await loop.run_in_executor(None, run_cli_process)
            agent_output = process_execution.stdout if process_execution.stdout else process_execution.stderr

            if not agent_output or not agent_output.strip():
                agent_output = "Done! Handled that cleanly."

            # Update session activity timestamp upon successful execution
            record_session_activity(channel)

            # Scrub secrets and credentials
            clean_output = scrub_secrets(agent_output)

            # Chunk and send messages cleanly
            chunks = chunk_message(clean_output, max_chars=1900)
            for chunk in chunks:
                await send_response_func(chunk)

        except subprocess.TimeoutExpired:
            evict_session(channel)
            await send_response_func(f"⚠️ Execution timed out after {AGENT_TIMEOUT_SECONDS}s. Context session cleared.")
        except Exception as err:
            evict_session(channel)
            await send_response_func(f"❌ Error: `{str(err)}`. Context session cleared.")
        finally:
            typing_task.cancel()


# --- SRE Commands (!agent & /agent) ---

@bot.command(name="agent")
async def prefix_agent(ctx, *, prompt: str = ""):
    """Prefix command !agent for SRE operators."""
    if not prompt:
        await ctx.send("Usage: `!agent <prompt>` or `!agent reset`")
        return
    await handle_agent_execution(
        channel=ctx.channel,
        author=ctx.author,
        prompt=prompt,
        send_response_func=ctx.send,
        reply_status_func=ctx.send,
    )


@bot.tree.command(name="agent", description="Execute an SRE agent command on the Proxmox host")
@app_commands.describe(prompt="The engineering task or question for the SRE agent")
async def slash_agent(interaction: discord.Interaction, prompt: str):
    """Slash command /agent for SRE operators."""
    if not is_admin_user(interaction.user):
        await interaction.response.send_message("⛔ Permission denied: this command requires the homelab admin role.", ephemeral=True)
        return

    category_id = getattr(interaction.channel, "category_id", 0)
    if REQUEST_CATEGORY_ID and category_id == REQUEST_CATEGORY_ID:
        ops_hint = f"<#{OPS_CHANNEL_ID}>" if OPS_CHANNEL_ID else "your ops channel"
        await interaction.response.send_message(
            f"⛔ SRE Agent commands are not permitted in household media request channels. Please use {ops_hint} instead.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    await handle_agent_execution(
        channel=interaction.channel,
        author=interaction.user,
        prompt=prompt,
        send_response_func=interaction.followup.send,
    )


if __name__ == "__main__":
    check_config()
    bot.run(DISCORD_TOKEN)
