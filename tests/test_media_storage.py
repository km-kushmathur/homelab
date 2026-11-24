import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import arr_cli
import media_storage
import sync_subtitles


UUID = "12345678-90ab-4cde-8fab-1234567890ab"


class StorageTests(unittest.TestCase):
    def test_probe_cleans_up_its_temp_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            mount = Path(temporary)
            for name in ("downloads", "movies", "tv"):
                (mount / name).mkdir()
            marker = mount / ".homelab-media-identity"
            marker.write_text(UUID)
            identity = {"uuid": UUID, "target": temporary, "id": 7, "fstype": "ext4", "options": "rw"}
            native_stat = os.stat
            device = native_stat(mount).st_dev

            def fixture_stat(path, *args, **kwargs):
                if path == f"/dev/disk/by-uuid/{UUID}":
                    return SimpleNamespace(st_rdev=device)
                return native_stat(path, *args, **kwargs)

            with (
                patch.object(media_storage, "storage_settings", return_value=(temporary, UUID)),
                patch.object(media_storage, "mount_identity", return_value=identity),
                patch.object(os.path, "ismount", return_value=True),
                patch.object(os, "stat", side_effect=fixture_stat),
            ):
                result = media_storage.inspect_storage()
                self.assertEqual(result["mount_id"], 7)
                self.assertEqual(result["device"], f"{os.major(device)}:{os.minor(device)}")
                self.assertFalse(list(mount.glob(".homelab-health-*")))
                marker.write_text("wrong filesystem")
                with self.assertRaisesRegex(media_storage.StorageUnavailable, "marker"):
                    media_storage.inspect_storage()
                self.assertFalse(list(mount.glob(".homelab-health-*")))

    def test_only_matching_cached_mount_is_known_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "last-good.json"
            record.write_text(json.dumps({
                "uuid": UUID, "mountpoint": "/mnt/media", "mount_id": 7,
                "device": "8:1", "fstype": "ext4",
            }))
            with (
                patch.object(media_storage, "LAST_GOOD", record),
                patch.object(media_storage, "storage_settings", return_value=("/mnt/media", UUID)),
            ):
                self.assertTrue(media_storage.known_stale_mount(7, "8:1", "ext4"))
                self.assertFalse(media_storage.known_stale_mount(7, "8:2", "ext4"))
                self.assertFalse(media_storage.known_stale_mount(8, "8:1", "ext4"))
                self.assertFalse(media_storage.known_stale_mount(7, "8:1", "xfs"))

    def test_acquisition_stops_before_network_when_storage_is_unavailable(self):
        with (
            patch.object(arr_cli, "require_media_storage", side_effect=media_storage.StorageUnavailable),
            patch.object(arr_cli, "make_request") as network,
        ):
            with self.assertRaises(media_storage.StorageUnavailable):
                arr_cli.cmd_jellyseerr_request(argparse.Namespace(id="1", media_type="movie"))
            network.assert_not_called()

    def test_capacity_reserve_and_probe_timeout(self):
        low_space = subprocess.CompletedProcess([], 0, stdout=json.dumps({"free_bytes": 14 * media_storage.GIB}))
        with patch.object(media_storage.subprocess, "run", return_value=low_space):
            with self.assertRaisesRegex(media_storage.StorageUnavailable, "at least 15 GiB"):
                media_storage.require_media_storage(15)
        with patch.object(media_storage.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 15)):
            with self.assertRaisesRegex(media_storage.StorageUnavailable, "verification failed"):
                media_storage.require_media_storage()

    def test_subtitle_dry_run_skips_refresh_and_sync(self):
        with (
            patch.object(sync_subtitles, "require_media_storage"),
            patch.object(sync_subtitles, "fetch_jellyfin_items", return_value=[]),
            patch.object(sync_subtitles, "fetch_jellyfin_series", return_value={}),
            patch.object(sync_subtitles, "refresh_jellyfin") as refresh,
            patch.object(sync_subtitles, "trigger_bazarr_tasks") as bazarr,
            patch("builtins.print"),
        ):
            sync_subtitles.sync_all_subtitles(dry_run=True)
            refresh.assert_not_called()
            bazarr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
