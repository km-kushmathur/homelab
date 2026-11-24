#!/usr/bin/env python3
"""Verify the configured ext4 media filesystem on the Proxmox host."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

LAST_GOOD = Path("/run/homelab-media-last-good.json")
GIB = 1024 ** 3
MIN_FREE_GIB = 15
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class StorageUnavailable(RuntimeError):
    pass


def storage_settings():
    config = Path(os.environ.get("MEDIA_STORAGE_CONFIG", "/etc/homelab/media-storage.json"))
    settings = json.loads(config.read_text())
    mount = Path(settings["mountpoint"])
    if not mount.is_absolute() or mount == Path("/") or ".." in mount.parts or "\n" in str(mount):
        raise StorageUnavailable("Configure a dedicated media mountpoint")
    return str(mount), str(UUID(settings["uuid"]))


def mount_identity(mount):
    result = subprocess.run(
        ["findmnt", "--json", "--mountpoint", mount, "-o", "TARGET,UUID,FSTYPE,OPTIONS,ID"],
        check=True, capture_output=True, text=True, timeout=3,
    )
    entries = json.loads(result.stdout)["filesystems"]
    if len(entries) != 1 or entries[0]["target"] != mount:
        raise StorageUnavailable("Ambiguous media mount")
    return entries[0]


def inspect_storage():
    mount, expected = storage_settings()
    if not os.path.ismount(mount):
        raise StorageUnavailable("Media filesystem is absent")
    identity = mount_identity(mount)
    if identity["uuid"] != expected or identity["fstype"] != "ext4" or "rw" not in identity["options"].split(","):
        raise StorageUnavailable("Incorrect media filesystem or write access")

    directory = os.open(mount, DIRECTORY_FLAGS)
    try:
        device = os.fstat(directory).st_dev
        if os.stat("/dev/disk/by-uuid/" + expected).st_rdev != device:
            raise StorageUnavailable("Media block device does not match mount")

        for name in ("downloads", "movies", "tv"):
            dataset = os.open(name, DIRECTORY_FLAGS, dir_fd=directory)
            try:
                if os.fstat(dataset).st_dev != device:
                    raise StorageUnavailable(f"Unexpected nested filesystem at {name}")
                with os.scandir(dataset) as entries:
                    next(entries, None)
            finally:
                os.close(dataset)

        marker_fd = os.open(".homelab-media-identity", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(marker_fd) as marker:
            if os.fstat(marker.fileno()).st_dev != device or marker.read(64).strip() != expected:
                raise StorageUnavailable("Media identity marker is invalid")

        # Anchor creation and cleanup to the verified mount even if its path changes.
        probe = f".homelab-health-{uuid4().hex}"
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(b"homelab media health\n")
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.unlink(probe, dir_fd=directory)
        os.fsync(directory)

        if mount_identity(mount) != identity or os.stat(mount).st_dev != device:
            raise StorageUnavailable("Media mount changed during probe")
        space = os.fstatvfs(directory)
    finally:
        os.close(directory)

    return {
        "uuid": expected,
        "mountpoint": mount,
        "fstype": "ext4",
        "mount_id": identity["id"],
        "device": f"{os.major(device)}:{os.minor(device)}",
        "total_bytes": space.f_blocks * space.f_frsize,
        "free_bytes": space.f_bavail * space.f_frsize,
    }


def known_stale_mount(mount_id, device, fstype):
    """Recognize a previously verified mount that lost its UUID lookup."""
    mount, expected = storage_settings()
    previous = json.loads(LAST_GOOD.read_text())
    return (
        mount_id > 0 and previous["mount_id"] == mount_id
        and fstype == previous["fstype"] == "ext4"
        and bool(device) and previous["device"] == device
        and previous["uuid"] == expected and previous["mountpoint"] == mount
    )


def require_media_storage(min_free_gib=0):
    if Path(os.environ.get("MAINTENANCE_LOCK", "/tmp/maintenance.lock")).exists():
        raise StorageUnavailable("Media storage is under maintenance")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--json"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        state = json.loads(result.stdout)
        free_bytes = state["free_bytes"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise StorageUnavailable("Media mount identity or health verification failed") from exc
    if free_bytes < min_free_gib * GIB:
        raise StorageUnavailable(f"Media storage requires at least {min_free_gib:g} GiB free")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--json", action="store_true")
    action.add_argument("--settings", action="store_true")
    action.add_argument("--known-stale-mount", type=int, metavar="MOUNT_ID")
    parser.add_argument("--device", metavar="MAJOR:MINOR")
    parser.add_argument("--filesystem-type")
    args = parser.parse_args()

    try:
        if args.settings:
            print(*storage_settings(), sep="\n")
            return 0
        if args.known_stale_mount is not None:
            return 0 if known_stale_mount(args.known_stale_mount, args.device, args.filesystem_type) else 1
        state = inspect_storage()
    except (StorageUnavailable, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, StorageUnavailable) else "Check the storage configuration and device"
        print(f"Media storage verification failed. {reason}.", file=sys.stderr)
        return 1
    print(json.dumps(state) if args.json else f"Verified ext4 media mount at {state['mountpoint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
