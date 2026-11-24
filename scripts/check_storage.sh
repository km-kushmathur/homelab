#!/bin/bash
# Run on the Proxmox host via storage-watchdog.timer.
set -euo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# The systemd unit loads the same file for scheduled runs. Read it here as well
# for operators invoking this script directly.
ENV_FILE="${HOMELAB_ENV_FILE:-/etc/homelab/homelab.env}"
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r name value || [[ -n "$name" ]]; do
        case "$name" in
            WATCHDOG_CT_IDS|MAINTENANCE_LOCK|STOP_TIMEOUT|NTFY_BASE_URL|NTFY_TOPIC|MEDIA_STORAGE_CONFIG)
                case "$value" in
                    \"*\"|\'*\') value=${value:1:${#value}-2} ;;
                esac
                if ! printenv "$name" >/dev/null; then
                    export "$name=$value"
                fi
                ;;
        esac
    done < "$ENV_FILE"
fi

GUARD="$(dirname -- "$0")/media_storage.py"
LAST_GOOD=/run/homelab-media-last-good.json
STATE=/run/homelab-storage-recovery.ids
read -r -a CT_IDS <<< "${WATCHDOG_CT_IDS:-201 204}"
MAINTENANCE_LOCK="${MAINTENANCE_LOCK:-/tmp/maintenance.lock}"
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

maintenance() { [[ -e "$MAINTENANCE_LOCK" ]]; }

healthy() {
    local result
    result=$(mktemp /run/.homelab-media-check.XXXXXX) || return 1
    if timeout 20 python3 "$GUARD" --json >"$result"; then
        mv "$result" "$LAST_GOOD"
        return
    fi
    unlink "$result"
    return 1
}

notify() {
    echo "$1" >&2
    if [[ -n "${NTFY_TOPIC:-}" ]]; then
        local endpoint="${NTFY_BASE_URL:-https://ntfy.sh}"
        curl -fsS --max-time 5 -d "$1" "${endpoint%/}/${NTFY_TOPIC}" >/dev/null 2>&1 || true
    fi
}

fail() { notify "CRITICAL: $*. Automatic recovery stopped."; exit 1; }

maintenance && exit 0
if healthy; then
    # Do not start workloads merely because storage came back after a failed run.
    if [[ -e "$STATE" ]]; then
        unlink "$STATE"
    fi
    exit 0
fi

notify "Media storage did not pass verification. Stopping dependent containers."
if [[ ! -e "$STATE" ]]; then
    running=()
    for id in "${CT_IDS[@]}"; do
        status=$(pct status "$id") || fail "Cannot determine container $id state"
        if [[ "$status" == 'status: running' ]]; then
            running+=("$id")
        fi
    done
    state_tmp=$(mktemp /run/.homelab-storage-recovery.XXXXXX)
    if (( ${#running[@]} )); then
        printf '%s\n' "${running[@]}" >"$state_tmp"
    fi
    mv "$state_tmp" "$STATE"
fi

for id in "${CT_IDS[@]}"; do
    maintenance && exit 0
    status=$(pct status "$id") || fail "Cannot determine container $id state"
    if [[ "$status" == 'status: running' ]]; then
        pct shutdown "$id" --timeout "$STOP_TIMEOUT" || fail "Graceful shutdown failed for container $id"
    fi
    [[ "$(pct status "$id")" == 'status: stopped' ]] || fail "Container $id is not stopped"
done

maintenance && exit 0
settings=$(python3 "$GUARD" --settings) || fail 'Invalid media storage configuration'
mount_point=${settings%$'\n'*}
expected=${settings##*$'\n'}
if mountpoint -q "$mount_point"; then
    actual=$(findmnt -n -M "$mount_point" -o UUID) || fail 'Cannot identify mounted filesystem'
    if [[ "$actual" != "$expected" ]]; then
        [[ -z "$actual" ]] || fail 'A different filesystem is mounted'
        mount_id=$(findmnt -n -M "$mount_point" -o ID) || fail 'Cannot identify stale mount'
        device=$(findmnt -n -M "$mount_point" -o MAJ:MIN) || fail 'Cannot identify stale device'
        fstype=$(findmnt -n -M "$mount_point" -o FSTYPE) || fail 'Cannot identify stale filesystem type'
        python3 "$GUARD" --known-stale-mount "$mount_id" --device "$device" --filesystem-type "$fstype" \
            || fail 'The stale mount was not previously verified'
    fi
    umount "$mount_point" || fail 'Clean unmount failed'
fi

maintenance && exit 0
mount "$mount_point" || fail 'Mount failed'
healthy || fail 'The recovered filesystem did not pass verification'
while IFS= read -r id; do
    maintenance && exit 0
    configured=false
    for ct in "${CT_IDS[@]}"; do
        if [[ "$id" == "$ct" ]]; then
            configured=true
            break
        fi
    done
    "$configured" || fail 'Invalid container in recovery state'
    healthy || fail 'Storage failed verification before restart'
    pct start "$id" || fail "Restart failed for container $id"
done <"$STATE"
unlink "$STATE"
notify "Media storage recovered. Previously running containers were restarted."
