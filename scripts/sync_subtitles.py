#!/usr/bin/env python3
"""
Subtitle Synchronization & Remediation Engine.

Ensures every piece of media has at least English subtitles:
- Identifies missing English subtitles across Movies and TV Shows via Jellyfin API.
- Automatically downloads English subtitles using high-efficiency providers (Gestdown for TV, YIFY & SubDL for Movies).
- Saves standardized external subtitles (<filename>.en.srt) with configured PUID:PGID ownership.
- Refreshes Jellyfin library and triggers Bazarr search tasks.

Configuration is environment-driven via /etc/homelab/homelab.env.
"""

import os
import sys
import re
import io
import time
import json
import zipfile
from pathlib import Path
from uuid import uuid4
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Optional, Tuple, Any

from media_storage import DIRECTORY_FLAGS, StorageUnavailable, require_media_storage

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
                    v = v.strip().strip("\"'")
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to load env file {filepath}: {e}\n")


load_env_file()

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://192.168.1.11:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
BAZARR_URL = os.environ.get("BAZARR_URL", "http://192.168.1.14:6767").rstrip("/")
BAZARR_API_KEY = os.environ.get("BAZARR_API_KEY", "")
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://192.168.1.14:8191/v1")

PUID = int(os.environ.get("PUID", "1000"))
PGID = int(os.environ.get("PGID", "1000"))


def fetch_jellyfin_items() -> List[Dict[str, Any]]:
    """Fetches all movies and episodes from Jellyfin."""
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    url = f"{JELLYFIN_URL}/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=MediaStreams,Path,ProviderIds,SeriesName,IndexNumber,ParentIndexNumber"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("Items", [])


def fetch_jellyfin_series() -> Dict[str, str]:
    """Returns mapping of series name to TVDB ID from Jellyfin."""
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    url = f"{JELLYFIN_URL}/Items?Recursive=true&IncludeItemTypes=Series&Fields=ProviderIds"
    req = urllib.request.Request(url, headers=headers)
    mapping = {}
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        for s in data.get("Items", []):
            name = s.get("Name")
            tvdb = s.get("ProviderIds", {}).get("Tvdb")
            if name and tvdb:
                mapping[name] = str(tvdb)
    return mapping


def item_has_english_subtitle(item: Dict[str, Any]) -> bool:
    """Checks if Jellyfin item already has an English subtitle track (embedded or external)."""
    streams = item.get("MediaStreams", [])
    for stream in streams:
        if stream.get("Type") == "Subtitle":
            lang = (stream.get("Language") or "").lower()
            if lang in ["eng", "en", "english"]:
                return True
    return False


def get_srt_path(video_path: str) -> str:
    """Returns the standardized external English subtitle path."""
    base, _ = os.path.splitext(video_path)
    return f"{base}.en.srt"


def write_subtitle(dest_path: str, content: bytes) -> None:
    storage = require_media_storage()
    mount = Path(storage["mountpoint"])
    dest = Path(dest_path)
    parent = dest.parent.resolve()
    if not parent.is_relative_to(mount):
        raise StorageUnavailable("Subtitle destination is outside the verified media filesystem")
    directory = os.open(parent, DIRECTORY_FLAGS)
    try:
        device = os.fstat(directory).st_dev
        if f"{os.major(device)}:{os.minor(device)}" != storage["device"]:
            raise StorageUnavailable("Media filesystem changed before the subtitle write")
        temp = f".subtitle-{uuid4().hex}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                os.fchown(output.fileno(), PUID, PGID)
                os.fchmod(output.fileno(), 0o664)
            os.replace(temp, dest.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            os.unlink(temp, dir_fd=directory)
            raise
    finally:
        os.close(directory)


def download_tv_subtitle_gestdown(show_tvdb_id: str, season: int, episode: int, dest_path: str) -> bool:
    """Downloads English subtitle for a TV episode using Gestdown API."""
    try:
        req_show = urllib.request.Request(
            f"https://api.gestdown.info/shows/external/tvdb/{show_tvdb_id}",
            headers={"User-Agent": "Bazarr"}
        )
        with urllib.request.urlopen(req_show, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            shows = data.get("shows", [])
            if not shows:
                return False
            show_id = shows[0]["id"]

        req_sub = urllib.request.Request(
            f"https://api.gestdown.info/subtitles/get/{show_id}/{season}/{episode}/eng",
            headers={"User-Agent": "Bazarr"}
        )
        with urllib.request.urlopen(req_sub, timeout=15) as resp:
            sub_data = json.loads(resp.read().decode("utf-8"))
            matching = sub_data.get("matchingSubtitles", [])
            if not matching:
                return False
            dl_uri = matching[0]["downloadUri"]

        req_dl = urllib.request.Request(
            f"https://api.gestdown.info{dl_uri}",
            headers={"User-Agent": "Bazarr"}
        )
        with urllib.request.urlopen(req_dl, timeout=20) as resp:
            content = resp.read()
            if content and len(content) > 100:
                write_subtitle(dest_path, content)
                return True
    except StorageUnavailable:
        raise
    except Exception as e:
        sys.stderr.write(f"Gestdown error for TVDB {show_tvdb_id} S{season:02d}E{episode:02d}: {e}\n")
    return False


def download_movie_subtitle_yify(imdb_id: str, dest_path: str) -> bool:
    """Downloads English subtitle for a movie from YIFY Subtitles."""
    try:
        url = f"https://yifysubtitles.ch/movie-imdb/{imdb_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        matches = re.findall(r"sub-lang\">English</span>.*?<a href=\"(/subtitles/[^\"]+)\"", html, re.DOTALL)
        if not matches:
            return False

        sub_page = f"https://yifysubtitles.ch{matches[0]}"
        req2 = urllib.request.Request(sub_page, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            html2 = resp2.read().decode("utf-8", errors="ignore")

        dl_match = re.search(r"class=\"[^\"]*download-subtitle[^\"]*\"[^>]*href=\"([^\"]+)\"", html2)
        if not dl_match:
            dl_match = re.search(r"href=\"([^\"]+download-subtitle[^\"]*|/subtitle/[^\"]+\.zip)\"", html2)
        if not dl_match:
            return False

        dl_url = dl_match.group(1)
        if not dl_url.startswith("http"):
            dl_url = f"https://yifysubtitles.ch{dl_url}"

        req3 = urllib.request.Request(dl_url, headers={"User-Agent": "Mozilla/5.0", "Referer": sub_page})
        with urllib.request.urlopen(req3, timeout=20) as resp3:
            zf = zipfile.ZipFile(io.BytesIO(resp3.read()))
            for name in zf.namelist():
                if name.lower().endswith(".srt"):
                    content = zf.read(name)
                    if content and len(content) > 100:
                        write_subtitle(dest_path, content)
                        return True
    except StorageUnavailable:
        raise
    except Exception as e:
        sys.stderr.write(f"YIFY error for IMDb {imdb_id}: {e}\n")
    return False


def download_movie_subtitle_subdl(query: str, dest_path: str) -> bool:
    """Downloads English subtitle from SubDL via Flaresolverr bypass."""
    try:
        search_query = urllib.parse.quote(query)
        payload = {
            "cmd": "request.get",
            "url": f"https://subdl.com/search/{search_query}",
            "maxTimeout": 30000
        }
        req_flare = urllib.request.Request(
            FLARESOLVERR_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_flare, timeout=35) as resp:
            flare_data = json.loads(resp.read().decode("utf-8"))
            html = flare_data.get("solution", {}).get("response", "")

        links = list(set(re.findall(r"href=\"(/subtitle/sd\d+/[^\"]+)\"", html)))
        if not links:
            return False

        sub_page_url = f"https://subdl.com{links[0]}"
        payload2 = {
            "cmd": "request.get",
            "url": sub_page_url,
            "maxTimeout": 30000
        }
        req_flare2 = urllib.request.Request(
            FLARESOLVERR_URL,
            data=json.dumps(payload2).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_flare2, timeout=35) as resp2:
            flare_data2 = json.loads(resp2.read().decode("utf-8"))
            solution2 = flare_data2.get("solution", {})
            html2 = solution2.get("response", "")
            cookies = {c["name"]: c["value"] for c in solution2.get("cookies", [])}
            ua = solution2.get("userAgent", "Mozilla/5.0")

        dl_links = re.findall(r"href=\"(https://dl\.subdl\.com/subtitle/[^\"]+\.zip)\"", html2)
        if not dl_links:
            return False

        cookie_header = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        req_dl = urllib.request.Request(
            dl_links[0],
            headers={
                "User-Agent": ua,
                "Referer": sub_page_url,
                "Cookie": cookie_header
            }
        )
        with urllib.request.urlopen(req_dl, timeout=25) as resp3:
            zf = zipfile.ZipFile(io.BytesIO(resp3.read()))
            for name in zf.namelist():
                if name.lower().endswith(".srt"):
                    content = zf.read(name)
                    if content and len(content) > 100:
                        write_subtitle(dest_path, content)
                        return True
    except StorageUnavailable:
        raise
    except Exception as e:
        sys.stderr.write(f"SubDL error for query '{query}': {e}\n")
    return False


def refresh_jellyfin() -> None:
    """Triggers library rescan in Jellyfin."""
    try:
        headers = {"X-Emby-Token": JELLYFIN_API_KEY}
        req = urllib.request.Request(f"{JELLYFIN_URL}/Library/Refresh", data=b"", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        sys.stderr.write(f"Failed to refresh Jellyfin: {e}\n")


def trigger_bazarr_tasks() -> None:
    """Triggers Bazarr missing subtitles searches."""
    if not BAZARR_API_KEY:
        return
    try:
        headers = {"X-API-KEY": BAZARR_API_KEY}
        for task in ["wanted_search_missing_subtitles_movies", "wanted_search_missing_subtitles_series"]:
            req = urllib.request.Request(f"{BAZARR_URL}/api/system/tasks?taskid={task}", data=b"", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    pass
            except Exception:
                pass
    except Exception as e:
        sys.stderr.write(f"Failed to trigger Bazarr tasks: {e}\n")


def sync_all_subtitles(dry_run: bool = False) -> Dict[str, Any]:
    """Orchestrates subtitle synchronization across all media."""
    require_media_storage()

    items = fetch_jellyfin_items()
    series_map = fetch_jellyfin_series()

    total_items = len(items)
    with_eng_initial = sum(1 for item in items if item_has_english_subtitle(item))
    missing_initial = [item for item in items if not item_has_english_subtitle(item)]

    print(f"Total media items: {total_items}")
    print(f"Items with English subtitles: {with_eng_initial}/{total_items} ({with_eng_initial/total_items*100:.1f}%)" if total_items else "No media items found.")
    print(f"Items missing English subtitles: {len(missing_initial)}")

    downloaded = 0
    skipped_existing_file = 0
    failed = []

    for item in missing_initial:
        name = item.get("Name", "Unknown")
        item_type = item.get("Type")
        video_path = item.get("Path")

        if not video_path or not os.path.exists(video_path):
            failed.append({"name": name, "reason": f"File does not exist: {video_path}"})
            continue

        srt_path = get_srt_path(video_path)
        if os.path.exists(srt_path) and os.path.getsize(srt_path) > 100:
            skipped_existing_file += 1
            continue

        if dry_run:
            print(f"[DRY RUN] Would download English subtitle for: [{item_type}] {name} -> {srt_path}")
            continue

        success = False
        if item_type == "Episode":
            series_name = item.get("SeriesName", "")
            season = item.get("ParentIndexNumber", 1)
            episode = item.get("IndexNumber", 1)
            tvdb_id = series_map.get(series_name)
            if tvdb_id:
                success = download_tv_subtitle_gestdown(tvdb_id, season, episode, srt_path)
            else:
                success = False

        elif item_type == "Movie":
            imdb_id = item.get("ProviderIds", {}).get("Imdb")
            if imdb_id:
                success = download_movie_subtitle_yify(imdb_id, srt_path)

            if not success:
                success = download_movie_subtitle_subdl(name, srt_path)

        if success:
            downloaded += 1
            print(f"[SUCCESS] Downloaded subtitle for [{item_type}] {name} -> {srt_path}")
        else:
            failed.append({"name": name, "type": item_type, "path": video_path})
            print(f"[FAILED] Could not locate subtitle for [{item_type}] {name}")

        time.sleep(0.1)

    if not dry_run:
        require_media_storage()
        print("\nTriggering Jellyfin library refresh...")
        refresh_jellyfin()
        trigger_bazarr_tasks()

    return {
        "total_items": total_items,
        "initial_covered": with_eng_initial,
        "initial_missing": len(missing_initial),
        "downloaded": downloaded,
        "already_had_srt_file": skipped_existing_file,
        "failed_count": len(failed),
        "failed_items": failed
    }


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: python3 sync_subtitles.py [--dry-run]")
        print("Synchronizes and fetches missing English subtitles for Jellyfin media library.")
        sys.exit(0)
    dry_run = "--dry-run" in sys.argv
    try:
        res = sync_all_subtitles(dry_run=dry_run)
    except StorageUnavailable as exc:
        sys.exit(str(exc))
    print("\n=== Subtitle Sync Summary ===")
    print(json.dumps(res, indent=2))
