#!/bin/bash
# Homelab storage watchdog.
#
# Detects when the shared media mount has dropped, safely stops the containers
# that depend on it, cycles the mount, restarts them, and triggers a media
# library rescan -- notifying at each step.
#
# Why this exists: when a mount disappears from under a running container, the
# container does not error. It sees an empty directory and happily keeps
# writing -- into the host's root filesystem. A downloader left in that state
# will fill the boot disk. Detecting the drop quickly matters more than
# recovering elegantly.
#
# Deploy to /opt/homelab/scripts/check_storage.sh and drive it from
# storage-watchdog.timer (see config/templates/systemd/).
#
# Full runbook: docs/admin_storage_watchdog.md

set -uo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ENV_FILE="${HOMELAB_ENV_FILE:-/etc/homelab/homelab.env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
fi

# --- Configuration (all overridable from the environment file) --------------
MOUNT_POINT="${MEDIA_ROOT:-/mnt/media}"
read -r -a CT_IDS <<< "${WATCHDOG_CT_IDS:-201 204}"
MEDIA_SERVER_IP="${MEDIA_SERVER_IP:-}"
MEDIA_SERVER_PORT="${MEDIA_SERVER_PORT:-8096}"
NTFY_BASE_URL="${NTFY_BASE_URL:-https://ntfy.sh}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
JELLYFIN_API_KEY="${JELLYFIN_API_KEY:-}"
MAINTENANCE_LOCK="${MAINTENANCE_LOCK:-/tmp/maintenance.lock}"
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"
# ---------------------------------------------------------------------------

# Maintenance override: skip auto-recovery entirely while the lock is present,
# so deliberate maintenance is not fought by the watchdog.
if [ -f "$MAINTENANCE_LOCK" ]; then
    exit 0
fi

notify() {
    [ -n "$NTFY_TOPIC" ] || return 0
    curl -fsS -m 10 -d "$1" "${NTFY_BASE_URL%/}/${NTFY_TOPIC}" >/dev/null 2>&1 || true
}

fail() {
    echo "ERROR: $1" >&2
    notify "CRITICAL: $1. Auto-recovery aborted."
    exit 1
}

stop_containers() {
    for ct in "${CT_IDS[@]}"; do
        if [ "$(pct status "$ct" | awk '{print $2}')" != "running" ]; then
            continue
        fi
        pct stop "$ct" || fail "Failed to issue stop for container $ct"

        waited=0
        while [ "$(pct status "$ct" | awk '{print $2}')" != "stopped" ]; do
            sleep 2
            waited=$((waited + 2))
            if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
                fail "Container $ct did not stop within ${STOP_TIMEOUT}s"
            fi
        done
    done
}

start_containers() {
    for ct in "${CT_IDS[@]}"; do
        pct start "$ct" || fail "Failed to start container $ct"
    done
}

rescan_library() {
    # Optional: only attempted when both the address and a key are configured.
    [ -n "$MEDIA_SERVER_IP" ] && [ -n "$JELLYFIN_API_KEY" ] || return 0

    code=$(curl -s -o /dev/null -w "%{http_code}" -m 15 -X POST \
        -H "X-Emby-Token: ${JELLYFIN_API_KEY}" \
        "http://${MEDIA_SERVER_IP}:${MEDIA_SERVER_PORT}/Library/Refresh")

    if [ "$code" = "204" ]; then
        notify "Recovered: mount restored, containers restarted, library rescan started."
    else
        notify "Partial: mount restored and containers restarted, but the library rescan returned HTTP ${code}."
    fi
}

mountpoint -q "$MOUNT_POINT" && exit 0

notify "Alert: ${MOUNT_POINT} is not mounted. Stopping dependent containers for recovery."

stop_containers

# Cycle the mount twice with a sync in between. A single remount often
# reattaches a stale handle; the sync-and-recycle forces a clean one.
umount "$MOUNT_POINT" 2>/dev/null || true
mount "$MOUNT_POINT"    || fail "Initial mount of ${MOUNT_POINT} failed"
sync                    || fail "sync failed"
umount "$MOUNT_POINT"   || fail "Unmount during mount cycle failed"
mount "$MOUNT_POINT"    || fail "Final mount of ${MOUNT_POINT} failed"

mountpoint -q "$MOUNT_POINT" || fail "Mount commands succeeded but ${MOUNT_POINT} is still not a mountpoint"

start_containers
sleep 15
rescan_library
