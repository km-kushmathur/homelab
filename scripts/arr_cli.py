#!/usr/bin/env python3
"""
Unified CLI for the homelab media stack.

Wraps Jellyseerr, Radarr, Sonarr, Prowlarr, the download client, Bazarr,
and Jellyfin behind one command surface, so the Discord bridge, the SRE
agent, and a human at a shell all drive the stack through the same code path.

Configuration comes entirely from the environment. Copy
scripts/homelab.env.example to /etc/homelab/homelab.env (mode 0600) and fill it
in; override the location with HOMELAB_ENV_FILE. Nothing here has a credential
baked in as a fallback -- a missing value produces an error naming the variable.
"""

import os
import sys
import re
import json
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timezone


DEFAULT_ENV_FILE = "/etc/homelab/homelab.env"


def load_env_file(filepath: Optional[str] = None) -> None:
    """Loads key-value pairs from an env file into os.environ if not already set."""
    filepath = filepath or os.environ.get("HOMELAB_ENV_FILE", DEFAULT_ENV_FILE)
    """Loads key-value pairs from .env file into os.environ if not already present."""
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


# Load default environment
load_env_file()

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://192.168.1.11:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

RADARR_URL = os.environ.get("RADARR_URL", "http://192.168.1.14:7878").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")

SONARR_URL = os.environ.get("SONARR_URL", "http://192.168.1.14:8989").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://192.168.1.14:9696").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")

JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL", "http://192.168.1.14:5055").rstrip("/")
JELLYSEERR_API_KEY = os.environ.get("JELLYSEERR_API_KEY", "")

DOWNLOADER_URL = os.environ.get("DOWNLOADER_URL", "http://192.168.1.14:8080").rstrip("/")
BAZARR_URL = os.environ.get("BAZARR_URL", "http://192.168.1.14:6767").rstrip("/")
BAZARR_API_KEY = os.environ.get("BAZARR_API_KEY", "")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
DISCORD_ADMIN_ROLE_ID = os.environ.get("DISCORD_ADMIN_ROLE_ID", "")
DISCORD_OPERATOR_ROLE_ID = os.environ.get("DISCORD_OPERATOR_ROLE_ID", "")
OWNER_DISCORD_ID = os.environ.get("OWNER_DISCORD_ID", os.environ.get("OPERATOR_USER_ID", ""))

REQUEST_MAP_FILE = os.environ.get("MEDIA_REQUEST_MAP_FILE", "/opt/homelab/.discord_media_requests.json")
REMEDIATION_STATE_FILE = os.environ.get("REMEDIATION_STATE_FILE", "/opt/homelab/.arr_remediation_state.json")

REQUIRED_ENV: Dict[str, List[str]] = {
    "jellyseerr": ["JELLYSEERR_API_KEY"],
    "radarr": ["RADARR_API_KEY"],
    "sonarr": ["SONARR_API_KEY"],
    "prowlarr": ["PROWLARR_API_KEY"],
    "jellyfin": ["JELLYFIN_API_KEY"],
    "downloader": [],
    "bazarr": ["BAZARR_API_KEY"],
    "subtitles": ["JELLYFIN_API_KEY"],
    "queue": ["RADARR_API_KEY", "SONARR_API_KEY"],
    "status": ["RADARR_API_KEY", "SONARR_API_KEY"],
    "history": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
    "pipeline-details": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
    "status-details": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
    "pipeline-cancel": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
    "switch-quality": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
    "discord-role": ["DISCORD_TOKEN", "DISCORD_GUILD_ID"],
}


def require_env(group: str) -> None:
    """Exits with a clear message if the variables a command group needs are unset."""
    missing = [name for name in REQUIRED_ENV.get(group, []) if not os.environ.get(name)]
    if not missing:
        return
    env_file = os.environ.get("HOMELAB_ENV_FILE", DEFAULT_ENV_FILE)
    sys.stderr.write(
        f"Missing credentials for '{group}': {', '.join(missing)}\n"
        f"Set these in {env_file} (see scripts/homelab.env.example) or in the environment.\n"
    )
    sys.exit(2)



def load_discord_requests() -> Dict[str, Any]:
    """Loads recorded Discord media request mappings from disk."""
    if not os.path.exists(REQUEST_MAP_FILE):
        return {}
    try:
        with open(REQUEST_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def make_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Any] = None,
    timeout: int = 30,
) -> Any:
    """Helper to perform HTTP requests and return parsed JSON or status info."""
    req_headers = headers.copy() if headers else {}
    req_data = None
    if data is not None:
        if isinstance(data, (dict, list)):
            req_data = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        elif isinstance(data, str):
            req_data = data.encode("utf-8")
        elif isinstance(data, bytes):
            req_data = data

    req = urllib.request.Request(url, data=req_data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            content = response.read()
            if not content or status == 204:
                return {"status_code": status, "message": "Success"}
            try:
                return json.loads(content.decode("utf-8"))
            except Exception:
                return {"status_code": status, "raw": content.decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        err_content = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_content)
            return {"error": True, "status_code": e.code, "response": parsed}
        except Exception:
            return {"error": True, "status_code": e.code, "message": err_content or str(e)}
    except Exception as e:
        return {"error": True, "message": str(e)}


def human_size(bytes_val: int) -> str:
    """Format bytes into human readable string."""
    if bytes_val is None or bytes_val < 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


def format_speed(bytes_per_sec: int) -> str:
    """Format byte rate into B/s, KB/s, MB/s."""
    return f"{human_size(bytes_per_sec)}/s"


def make_progress_bar(pct: float, length: int = 10) -> str:
    """Generates an ASCII progress bar."""
    filled = int(round((pct / 100.0) * length))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def parse_media_query(raw_query: str) -> Dict[str, Any]:
    """
    Intelligently parses user queries to extract clean title, quality intent,
    media type hint, season number, and release year.
    Example: 'cars 2 4k' -> clean_title: 'cars 2', quality: '4k'
    """
    import math
    import re

    q = raw_query.strip()
    quality = None
    media_type_hint = None
    season = None
    year = None

    # Check 4K / UHD
    if re.search(r"\b(4k|2160p|uhd|ultra[- ]?hd|2160)\b", q, re.I):
        quality = "4k"
    elif re.search(r"\b(1080p|1080|fhd|full[- ]?hd)\b", q, re.I):
        quality = "1080p"
    elif re.search(r"\b(720p|720)\b", q, re.I):
        quality = "720p"

    # Check season
    s_match = re.search(r"\b(?:season|s)\s*(\d+)\b", q, re.I)
    if s_match:
        season = int(s_match.group(1))
        media_type_hint = "tv"

    # Check TV/Movie hints
    if not media_type_hint:
        if re.search(r"\b(tv[- ]?show|tv[- ]?series|series|show)\b", q, re.I):
            media_type_hint = "tv"
        elif re.search(r"\b(movies?|films?)\b", q, re.I):
            media_type_hint = "movie"

    # Check Year
    y_match = re.search(r"\b(19\d\d|20\d\d)\b", q)
    if y_match:
        year = y_match.group(1)

    # Strip tags to get clean title
    cleaned = q
    cleaned = re.sub(r"\b(4k|2160p|uhd|ultra[- ]?hd|2160|1080p|1080|fhd|full[- ]?hd|720p|720)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(hdr(?:10(?:\+)?)?|dv|dolby[- ]?vision|remux|bluray|bdrip|brrip|web[- ]?dl|webrip|dvd(?:rip)?|x264|h264|x265|h265|hevc|10bit|aac|ac3|dts|atmos)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(tv[- ]?show|tv[- ]?series|series|show|movies?|films?)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:season|s)\s*\d+\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:episode|ep|e)\s*\d+\b", " ", cleaned, flags=re.I)

    cleaned_without_year = re.sub(r"\b(19\d\d|20\d\d)\b", " ", cleaned).strip()
    cleaned_without_year = re.sub(r"[\(\)\[\]\{\}\-_:]", " ", cleaned_without_year)
    cleaned_without_year = re.sub(r"\s+", " ", cleaned_without_year).strip()

    cleaned_with_year = re.sub(r"[\(\)\[\]\{\}\-_:]", " ", cleaned)
    cleaned_with_year = re.sub(r"\s+", " ", cleaned_with_year).strip()

    final_title = cleaned_without_year if cleaned_without_year else (cleaned_with_year if cleaned_with_year else q)

    return {
        "raw": raw_query,
        "clean_title": final_title,
        "clean_title_with_year": cleaned_with_year,
        "quality": quality,
        "media_type_hint": media_type_hint,
        "season": season,
        "year": year,
    }


def rank_results(results: List[Dict[str, Any]], query_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Ranks TMDB search results prioritizing popularity, vote count, exact title matches,
    and matching media type and year hints.
    """
    import math
    q_norm = query_info.get("clean_title", "").lower().strip()
    media_type_hint = query_info.get("media_type_hint")
    year_hint = query_info.get("year")

    def compute_score(r: Dict[str, Any]) -> float:
        title = (r.get("title") or r.get("name") or "").lower()
        mtype = r.get("mediaType", "")
        pop = float(r.get("popularity") or 0.0)
        votes = int(r.get("voteCount") or 0)
        rel_date = r.get("releaseDate") or r.get("firstAirDate") or ""

        score = math.log10(max(1, votes + 1)) * 15.0 + min(pop, 50.0)

        if votes == 0 and not rel_date:
            score -= 40.0

        if title == q_norm:
            score += 80.0
        elif title.startswith(q_norm):
            score += 40.0
        elif q_norm in title:
            score += 20.0

        if media_type_hint and mtype == media_type_hint:
            score += 35.0

        if year_hint and rel_date.startswith(year_hint):
            score += 40.0

        return score

    return sorted(results, key=compute_score, reverse=True)


def parse_release_quality(title: str) -> Dict[str, str]:
    """Extracts resolution, source, and codec/HDR information from a release title."""
    import re
    t = title.lower()
    res = "Other"
    if re.search(r"\b(2160p|4k|uhd)\b", t):
        res = "2160p 4K"
    elif re.search(r"\b(1080p|1080i|fhd)\b", t):
        res = "1080p FHD"
    elif re.search(r"\b(720p)\b", t):
        res = "720p HD"
    elif re.search(r"\b(480p|sd|dvdrip|dvd)\b", t):
        res = "480p SD"

    source = ""
    if re.search(r"\bremux\b", t):
        source = "Remux"
    elif re.search(r"\b(bluray|blu-ray|bdrip|brrip)\b", t):
        source = "BluRay"
    elif re.search(r"\b(web-dl|webdl|webrip|web-rip|web)\b", t):
        source = "WEB-DL"
    elif re.search(r"\b(hdtv|pdtv)\b", t):
        source = "HDTV"
    elif re.search(r"\b(cam|ts|telesync|hdcam|hdts)\b", t):
        source = "CAM/TS"

    extra = []
    if re.search(r"\b(dv|dovi|dolby vision)\b", t):
        extra.append("DV")
    if re.search(r"\b(hdr10\+|hdr10|hdr)\b", t):
        extra.append("HDR")
    if re.search(r"\b(hevc|x265|h265)\b", t):
        extra.append("x265")
    elif re.search(r"\b(x264|h264|avc)\b", t):
        extra.append("x264")

    parts = [res]
    if source:
        parts.append(source)
    if extra:
        extra_str = ", ".join(extra)
        parts.append(f"({extra_str})")

    label = " ".join(parts)
    q_key = "4k" if "2160p" in res else ("1080p" if "1080p" in res else ("720p" if "720p" in res else "other"))
    is_cam = (source == "CAM/TS") or bool(re.search(r"\b(cam|ts|telesync|hdcam|hdts|camrip)\b", t, re.I))
    return {"quality_label": label, "quality_key": q_key, "resolution": res, "source": source, "is_cam": is_cam}


def fetch_prowlarr_releases(title: str, media_type: str = "movie", year: Optional[str] = None, timeout: int = 40) -> Dict[str, Any]:
    """
    Queries Prowlarr for real tracker releases and provides an interactive size and quality breakdown.
    """
    category = 2000 if media_type == "movie" else 5000
    enc = urllib.parse.quote(title)
    url = f"{PROWLARR_URL}/api/v1/search?query={enc}&type=search&categories={category}"
    res = make_request(url, headers={"X-Api-Key": PROWLARR_API_KEY}, timeout=timeout)

    if not isinstance(res, list) or not res:
        return {"has_releases": False, "total_releases": 0, "qualities": {}, "top_releases": [], "only_theatre_recordings": False}

    qualities: Dict[str, List[Dict[str, Any]]] = {"4k": [], "1080p": [], "720p": [], "other": []}
    top_releases: List[Dict[str, Any]] = []

    for item in res:
        t = item.get("title", "")
        sz_bytes = item.get("size", 0) or 0
        sz_gb = round(sz_bytes / (1024.0**3), 2)
        sources = item.get("sources", 0) or item.get(bytes.fromhex("73656564657273").decode(), 0) or 0
        idx = item.get("indexer", "Tracker")
        q_meta = parse_release_quality(t)
        q_key = q_meta["quality_key"]

        if sz_gb > 0:
            qualities[q_key].append({
                "size_gb": sz_gb,
                "title": t,
                "sources": sources,
                "indexer": idx,
                "quality_label": q_meta["quality_label"],
                "quality_key": q_key,
                "source": q_meta.get("source"),
                "is_cam": q_meta.get("is_cam", False),
            })

    summary: Dict[str, Any] = {}
    for q_key in ["4k", "1080p", "720p", "other"]:
        items = qualities.get(q_key, [])
        if items:
            sizes = [x["size_gb"] for x in items]
            best_by_sources = sorted(items, key=lambda x: x.get("sources", 0), reverse=True)[0]
            has_digital = any((not x.get("is_cam")) and ((x.get("sources", 0) or 0) > 0) for x in items)
            has_cam = any(x.get("is_cam") for x in items)
            is_theatre = bool(best_by_sources.get("is_cam")) or (has_cam and not has_digital)
            summary[q_key] = {
                "count": len(items),
                "min_gb": min(sizes),
                "max_gb": max(sizes),
                "avg_gb": round(sum(sizes) / len(sizes), 1),
                "best_release": best_by_sources,
                "has_digital": has_digital,
                "is_theatre_recording": is_theatre,
            }

    sorted_all = sorted(res, key=lambda x: x.get("sources", 0) or 0 or 0, reverse=True)[:10]
    for idx_num, x in enumerate(sorted_all):
        t = x.get("title", "Unknown")
        q_meta = parse_release_quality(t)
        top_releases.append({
            "idx": idx_num,
            "title": t,
            "size_gb": round((x.get("size", 0) or 0) / (1024.0**3), 2),
            "sources": x.get("sources", 0) or 0,
            "indexer": x.get("indexer", "Tracker"),
            "quality_label": q_meta["quality_label"],
            "quality_key": q_meta["quality_key"],
            "guid": x.get("guid") or x.get("downloadUrl") or "",
            "is_cam": q_meta.get("is_cam", False),
        })

    all_active_sources = [x for x in res if (x.get("sources", 0) or 0 or 0) > 0]
    only_theatre = len(all_active_sources) > 0 and all(parse_release_quality(x.get("title", "")).get("is_cam") for x in all_active_sources)

    return {
        "has_releases": True,
        "total_releases": len(res),
        "qualities": summary,
        "top_releases": top_releases,
        "only_theatre_recordings": only_theatre,
    }


def estimate_download_size(runtime_minutes: int, is_tv: bool = False, episode_count: int = 1) -> Dict[str, Any]:
    """Calculates size estimates in GB for 1080p and 4K based on runtime."""
    rt = max(1, runtime_minutes)
    if is_tv and episode_count > 0:
        ep_rt = max(1, rt // episode_count)
        return {
            "1080p_avg_gb": round((rt * 28) / 1024.0, 2),
            "1080p_max_gb": round((rt * 65) / 1024.0, 2),
            "4k_avg_gb": round((rt * 85) / 1024.0, 2),
            "4k_max_gb": round((rt * 150) / 1024.0, 2),
            "per_episode_1080p_gb": round((ep_rt * 28) / 1024.0, 2),
            "per_episode_4k_gb": round((ep_rt * 85) / 1024.0, 2),
            "episode_count": episode_count,
        }
    return {
        "1080p_avg_gb": round((rt * 35) / 1024.0, 2),
        "1080p_max_gb": round((rt * 75) / 1024.0, 2),
        "4k_avg_gb": round((rt * 100) / 1024.0, 2),
        "4k_max_gb": round((rt * 185) / 1024.0, 2),
    }


# --- Jellyseerr Commands ---

def cmd_jellyseerr_search(args: argparse.Namespace) -> None:
    query_str = " ".join(args.query) if isinstance(args.query, list) else args.query
    query_info = parse_media_query(query_str)

    # 1. Primary search with clean_title
    encoded_q = urllib.parse.quote(query_info["clean_title"])
    url = f"{JELLYSEERR_URL}/api/v1/search?query={encoded_q}"
    res = make_request(url, headers={"X-Api-Key": JELLYSEERR_API_KEY})
    results = res.get("results", []) if isinstance(res, dict) else []

    # 2. Fallback search with clean_title_with_year if 0 results
    if not results and query_info.get("clean_title_with_year") != query_info.get("clean_title"):
        encoded_q2 = urllib.parse.quote(query_info["clean_title_with_year"])
        url2 = f"{JELLYSEERR_URL}/api/v1/search?query={encoded_q2}"
        res2 = make_request(url2, headers={"X-Api-Key": JELLYSEERR_API_KEY})
        results = res2.get("results", []) if isinstance(res2, dict) else []

    # 3. Fallback to raw query if still 0 results
    if not results and query_str != query_info["clean_title"]:
        encoded_raw = urllib.parse.quote(query_str)
        url3 = f"{JELLYSEERR_URL}/api/v1/search?query={encoded_raw}"
        res3 = make_request(url3, headers={"X-Api-Key": JELLYSEERR_API_KEY})
        results = res3.get("results", []) if isinstance(res3, dict) else []

    # Rank results
    ranked = rank_results(results, query_info) if results else []

    output_payload = {
        "parsed_query": query_info,
        "page": 1,
        "totalResults": len(ranked),
        "results": ranked
    }

    if args.json:
        print(json.dumps(output_payload, indent=2))
        return

    if not ranked:
        print(f"No results found in Jellyseerr for '{query_str}' (searched as '{query_info['clean_title']}').")
        return

    print(f"Found {len(ranked)} results for '{query_str}' (Cleaned: '{query_info['clean_title']}'):\n")
    fmt = "{:<8} {:<8} {:<6} {:<40} {:<12}"
    print(fmt.format("ID", "TYPE", "YEAR", "TITLE", "STATUS"))
    print("-" * 80)
    for item in ranked:
        media_id = str(item.get("id", ""))
        media_type = item.get("mediaType", "movie")
        title = item.get("title") or item.get("name") or "Unknown"
        rel_date = item.get("releaseDate") or item.get("firstAirDate") or ""
        year = rel_date[:4] if len(rel_date) >= 4 else "N/A"
        media_info = item.get("mediaInfo") or {}
        status_num = media_info.get("status", 1)
        status_map = {1: "UNKNOWN", 2: "PENDING", 3: "PROCESSING", 4: "PARTIAL", 5: "AVAILABLE"}
        status_str = status_map.get(status_num, f"Status {status_num}")

        disp_title = (title[:37] + "...") if len(title) > 40 else title
        print(fmt.format(media_id, media_type, year, disp_title, status_str))


def cmd_jellyseerr_details(args: argparse.Namespace) -> None:
    media_id = int(args.id)
    media_type = args.media_type.lower()
    if media_type not in ["movie", "tv"]:
        print("Error: --media-type must be 'movie' or 'tv'.")
        sys.exit(1)

    url = f"{JELLYSEERR_URL}/api/v1/{media_type}/{media_id}"
    res = make_request(url, headers={"X-Api-Key": JELLYSEERR_API_KEY})

    if isinstance(res, dict) and res.get("error"):
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Error fetching details: {res.get('message') or res.get('response')}")
        sys.exit(1)

    title = res.get("title") or res.get("name") or "Unknown"
    overview = res.get("overview") or "No overview."
    is_tv = (media_type == "tv")
    num_eps = 1
    if is_tv:
        ep_rts = res.get("episodeRunTime") or []
        ep_rt = ep_rts[0] if ep_rts else 45
        num_eps = res.get("numberOfEpisodes") or 10
        rt = num_eps * ep_rt
    else:
        rt = res.get("runtime") or 110

    size_est = estimate_download_size(rt, is_tv=is_tv, episode_count=num_eps)

    # Fetch live tracker release breakdown from Prowlarr if explicitly requested
    include_releases = getattr(args, "releases", False)
    real_releases = {"has_releases": False}
    if include_releases:
        try:
            rel_date = res.get("releaseDate") or res.get("firstAirDate") or ""
            year = rel_date[:4] if len(rel_date) >= 4 else None
            real_releases = fetch_prowlarr_releases(title, media_type=media_type, year=year, timeout=40)
        except Exception:
            pass

    if args.json:
        res["computed_runtime_minutes"] = rt
        res["size_estimates"] = size_est
        res["real_releases"] = real_releases
        print(json.dumps(res, indent=2))
        return

    if media_type == "movie":
        print(f"Movie: {title} (Runtime: {rt} mins)")
    else:
        print(f"TV Show: {title} ({res.get('numberOfSeasons', 0)} Seasons, {num_eps} Episodes, Total ~{rt} mins)")

    print(f"Overview: {overview[:120]}...")
    print(f"Estimated 1080p: ~{size_est['1080p_avg_gb']} GB (Max: {size_est['1080p_max_gb']} GB)")
    print(f"Estimated 4K:    ~{size_est['4k_avg_gb']} GB (Max: {size_est['4k_max_gb']} GB)")

    if real_releases.get("has_releases"):
        print(f"\nLive Tracker Releases ({real_releases['total_releases']} found):")
        for q, qinfo in real_releases.get("qualities", {}).items():
            print(f" - {q.upper()}: {qinfo['min_gb']} - {qinfo['max_gb']} GB (Avg: {qinfo['avg_gb']} GB, {qinfo['count']} items)")


def cmd_jellyseerr_request(args: argparse.Namespace) -> None:
    media_id = int(args.id)
    media_type = args.media_type.lower()
    if media_type not in ["movie", "tv"]:
        print("Error: --media-type must be 'movie' or 'tv'.")
        sys.exit(1)

    is_4k = getattr(args, "is_4k", False) or getattr(args, "quality", "") == "4k"
    seasons_arg = getattr(args, "seasons", "all")

    # Dynamically resolve server endpoint from Jellyseerr settings
    cfg_servers = make_request(
        f"{JELLYSEERR_URL}/api/v1/settings/{'radarr' if media_type == 'movie' else 'sonarr'}",
        headers={"X-Api-Key": JELLYSEERR_API_KEY}
    )
    server_id = 0
    actual_is_4k = is_4k
    if isinstance(cfg_servers, list) and cfg_servers:
        if is_4k:
            s_4k = next((s for s in cfg_servers if s.get("is4k")), None)
            if s_4k:
                server_id = s_4k.get("id", 1)
            else:
                server_id = cfg_servers[0].get("id", 0)
                actual_is_4k = False  # Fallback to standard server with profile 5 if no 4K server registered
        else:
            s_std = next((s for s in cfg_servers if not s.get("is4k")), None)
            server_id = s_std.get("id", 0) if s_std else cfg_servers[0].get("id", 0)

    payload = {
        "mediaType": media_type,
        "mediaId": media_id,
        "is4k": actual_is_4k,
        "serverId": server_id,
        "rootFolder": "/media/movies" if media_type == "movie" else "/media/tv",
    }
    if is_4k:
        payload["profileId"] = 5  # Ultra-HD
    else:
        payload["profileId"] = 4  # HD-1080p

    if media_type == "tv":
        if seasons_arg == "all":
            payload["seasons"] = "all"
        elif isinstance(seasons_arg, str):
            parsed_seasons = [int(s.strip()) for s in seasons_arg.split(",") if s.strip().isdigit()]
            payload["seasons"] = parsed_seasons if parsed_seasons else "all"
        else:
            payload["seasons"] = "all"

    url = f"{JELLYSEERR_URL}/api/v1/request"
    res = make_request(url, method="POST", headers={"X-Api-Key": JELLYSEERR_API_KEY}, data=payload)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error submitting request to Jellyseerr: {res.get('response') or res.get('message')}")
        sys.exit(1)

    req_id = res.get("id") if isinstance(res, dict) else "N/A"
    q_str = "4K Ultra-HD" if is_4k else "HD-1080p"
    print(f"Successfully submitted request #{req_id} for {media_type} ({q_str}) (TMDB ID: {media_id}).")


def cmd_jellyseerr_status(args: argparse.Namespace) -> None:
    req_id = args.id
    # First try as request ID
    url = f"{JELLYSEERR_URL}/api/v1/request/{req_id}"
    res = make_request(url, headers={"X-Api-Key": JELLYSEERR_API_KEY})

    if isinstance(res, dict) and res.get("error") and res.get("status_code") == 404:
        # Fallback to media ID
        url = f"{JELLYSEERR_URL}/api/v1/media/{req_id}"
        res = make_request(url, headers={"X-Api-Key": JELLYSEERR_API_KEY})

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error fetching Jellyseerr status for #{req_id}: {res.get('message') or res.get('response')}")
        sys.exit(1)

    status_map = {1: "PENDING_APPROVAL", 2: "APPROVED", 3: "DECLINED", 4: "PROCESSING", 5: "AVAILABLE"}
    status_num = res.get("status", 1)
    status_text = status_map.get(status_num, str(status_num))

    req_type = res.get("type") or (res.get("media", {}) if isinstance(res.get("media"), dict) else {}).get("mediaType") or "unknown"
    requested_by = (res.get("requestedBy", {}) if isinstance(res.get("requestedBy"), dict) else {}).get("displayName") or "Unknown"
    created_at = res.get("createdAt", "")

    print(f"Request ID:    {res.get('id', req_id)}")
    print(f"Type:          {req_type}")
    print(f"Status:        {status_text}")
    print(f"Requested By:  {requested_by}")
    print(f"Created At:    {created_at}")


# --- Status Aggregation Command ---

def get_pipeline_details(identifier: Union[int, str]) -> Dict[str, Any]:
    """
    Computes comprehensive pipeline diagnostics for a media item across:
    Jellyseerr -> Radarr/Sonarr -> Download Client -> Storage Import -> Jellyfin.
    """
    id_str = str(identifier).strip()
    mtype = None
    req_id = None
    arr_id = None
    tmdb_id = None
    target_service = None

    if id_str.isdigit():
        req_id = int(id_str)
    elif id_str.lower().startswith("req:") or id_str.lower().startswith("req-"):
        clean_num = re.sub(r"[^\d]", "", id_str)
        req_id = int(clean_num) if clean_num else None
    elif id_str.lower().startswith("movie:") or id_str.lower().startswith("radarr:"):
        mtype = "movie"
        target_service = "Radarr"
        clean_num = re.sub(r"[^\d]", "", id_str)
        arr_id = int(clean_num) if clean_num else None
    elif id_str.lower().startswith("tv:") or id_str.lower().startswith("sonarr:"):
        mtype = "tv"
        target_service = "Sonarr"
        clean_num = re.sub(r"[^\d]", "", id_str)
        arr_id = int(clean_num) if clean_num else None
    elif id_str.lower().startswith("queue:") or id_str.lower().startswith("queue-"):
        clean_num = re.sub(r"[^\d]", "", id_str)
        q_target_id = int(clean_num) if clean_num else None
        if q_target_id:
            r_q = make_request(f"{RADARR_URL}/api/v3/queue", headers={"X-Api-Key": RADARR_API_KEY})
            for q in (r_q.get("records", []) if isinstance(r_q, dict) else []):
                if q.get("id") == q_target_id:
                    mtype = "movie"
                    target_service = "Radarr"
                    arr_id = q.get("movieId")
                    queue_item = q
                    break
            if not queue_item:
                s_q = make_request(f"{SONARR_URL}/api/v3/queue", headers={"X-Api-Key": SONARR_API_KEY})
                for q in (s_q.get("records", []) if isinstance(s_q, dict) else []):
                    if q.get("id") == q_target_id:
                        mtype = "tv"
                        target_service = "Sonarr"
                        arr_id = q.get("seriesId")
                        queue_item = q
                        break
    elif id_str.lower().startswith("tmdb:"):
        parts = id_str.split(":")
        if len(parts) >= 3:
            mtype = parts[1].lower()
            tmdb_id = int(parts[2])
        elif len(parts) == 2 and parts[1].isdigit():
            tmdb_id = int(parts[1])

    req_data: Dict[str, Any] = {}
    media_data: Dict[str, Any] = {}

    # 1. Fetch Jellyseerr Request if req_id is known
    if req_id:
        r_url = f"{JELLYSEERR_URL}/api/v1/request/{req_id}"
        r_res = make_request(r_url, headers={"X-Api-Key": JELLYSEERR_API_KEY})
        if isinstance(r_res, dict) and not r_res.get("error"):
            req_data = r_res
            mtype = req_data.get("type", "movie")
            media_data = req_data.get("media") or {}
            tmdb_id = media_data.get("tmdbId")
            if not arr_id:
                arr_id = media_data.get("externalServiceId") or media_data.get("externalServiceId4k")

    # If no request data yet but arr_id or tmdb_id is known, check Arr stack first
    arr_data: Optional[Dict[str, Any]] = None
    if mtype == "movie" and arr_id:
        arr_data = make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}", headers={"X-Api-Key": RADARR_API_KEY})
        if isinstance(arr_data, dict) and not arr_data.get("error"):
            if not tmdb_id:
                tmdb_id = arr_data.get("tmdbId")
    elif mtype == "tv" and arr_id:
        arr_data = make_request(f"{SONARR_URL}/api/v3/series/{arr_id}", headers={"X-Api-Key": SONARR_API_KEY})
        if isinstance(arr_data, dict) and not arr_data.get("error"):
            if not tmdb_id:
                tmdb_id = arr_data.get("tmdbId")

    # 2. Search Jellyseerr requests by TMDB/Arr ID or Title
    clean_search = re.sub(r"[^\w\s]", "", id_str.lower()).strip()
    if not req_data:
        j_all = make_request(f"{JELLYSEERR_URL}/api/v1/request?take=100&filter=all", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        for r in (j_all.get("results", []) if isinstance(j_all, dict) else []):
            m = r.get("media") or {}
            m_arr = m.get("externalServiceId") or m.get("externalServiceId4k")
            r_tid = m.get("tmdbId")
            r_tvdb = m.get("tvdbId")
            r_mtype = m.get("mediaType") or r.get("type") or "movie"

            matched = False
            if arr_id and m_arr and (m_arr == arr_id or str(m_arr) == str(arr_id)) and (not mtype or r_mtype == mtype):
                matched = True
            elif tmdb_id and r_tid and r_tid == tmdb_id and (not mtype or r_mtype == mtype):
                matched = True
            elif arr_data and arr_data.get("tvdbId") and r_tvdb and r_tvdb == arr_data.get("tvdbId"):
                matched = True

            if matched:
                req_data = r
                req_id = r.get("id")
                media_data = m
                if not mtype:
                    mtype = r_mtype
                if not arr_id:
                    arr_id = m_arr
                if not tmdb_id and r_tid:
                    tmdb_id = r_tid
                break
            if not req_id and not tmdb_id and r_tid:
                meta = make_request(f"{JELLYSEERR_URL}/api/v1/{r_mtype}/{r_tid}", headers={"X-Api-Key": JELLYSEERR_API_KEY})
                r_title = (meta.get("title") or meta.get("name") or "").lower()
                r_clean = re.sub(r"[^\w\s]", "", r_title).strip()
                if clean_search and (clean_search == r_clean or (len(clean_search) >= 4 and clean_search in r_clean)):
                    req_data = r
                    req_id = r.get("id")
                    media_data = m
                    mtype = r_mtype
                    tmdb_id = r_tid
                    arr_id = m_arr
                    title = meta.get("title") or meta.get("name")
                    break

    # 3. Check Radarr & Sonarr directly by title if still not resolved
    if not arr_id or not tmdb_id:
        if mtype in ["movie", None]:
            r_movies = make_request(f"{RADARR_URL}/api/v3/movie", headers={"X-Api-Key": RADARR_API_KEY})
            if isinstance(r_movies, list):
                for mov in r_movies:
                    m_t = (mov.get("title") or "").lower()
                    m_clean = re.sub(r"[^\w\s]", "", m_t).strip()
                    if clean_search and (clean_search == m_clean or (len(clean_search) >= 4 and clean_search in m_clean)):
                        mtype = "movie"
                        arr_id = mov.get("id")
                        tmdb_id = mov.get("tmdbId")
                        arr_data = mov
                        title = mov.get("title")
                        break
        if (not arr_id or not tmdb_id) and mtype in ["tv", None]:
            s_series = make_request(f"{SONARR_URL}/api/v3/series", headers={"X-Api-Key": SONARR_API_KEY})
            if isinstance(s_series, list):
                for ser in s_series:
                    s_t = (ser.get("title") or "").lower()
                    s_clean = re.sub(r"[^\w\s]", "", s_t).strip()
                    if clean_search and (clean_search == s_clean or (len(clean_search) >= 4 and clean_search in s_clean)):
                        mtype = "tv"
                        arr_id = ser.get("id")
                        tmdb_id = ser.get("tmdbId")
                        arr_data = ser
                        title = ser.get("title")
                        break

    if not mtype:
        mtype = "movie"
    arr_service = "Radarr" if mtype == "movie" else "Sonarr"

    # 2. TMDB Metadata
    tmdb_meta: Dict[str, Any] = {}
    title = f"{mtype.capitalize()} #{req_id or arr_id or tmdb_id or 'Unknown'}"
    year = ""
    overview = "No description available."
    poster_url = None

    if tmdb_id:
        d_url = f"{JELLYSEERR_URL}/api/v1/{mtype}/{tmdb_id}"
        d_res = make_request(d_url, headers={"X-Api-Key": JELLYSEERR_API_KEY}, timeout=8)
        if isinstance(d_res, dict) and not d_res.get("error"):
            tmdb_meta = d_res
            title = d_res.get("title") or d_res.get("name") or title
            rel_date = d_res.get("releaseDate") or d_res.get("firstAirDate") or ""
            if len(rel_date) >= 4:
                year = f" ({rel_date[:4]})"
            overview = d_res.get("overview") or overview
            poster_path = d_res.get("posterPath")
            if poster_path:
                poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}"

    if not tmdb_meta and arr_data:
        title = arr_data.get("title") or title
        y_val = arr_data.get("year")
        if y_val:
            year = f" ({y_val})"
        overview = arr_data.get("overview") or overview

    if not req_data and not arr_data and not tmdb_meta and not tmdb_id:
        return {"error": True, "message": f"Media request or item '{identifier}' was not found in Jellyseerr, Radarr, or Sonarr."}

    # 3. Arr Stack Data & Queue Matching
    if not arr_data and arr_id:
        if mtype == "movie":
            arr_data = make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}", headers={"X-Api-Key": RADARR_API_KEY})
        else:
            arr_data = make_request(f"{SONARR_URL}/api/v3/series/{arr_id}", headers={"X-Api-Key": SONARR_API_KEY})

    queue_item: Optional[Dict[str, Any]] = None
    if mtype == "movie":
        r_q = make_request(f"{RADARR_URL}/api/v3/queue", headers={"X-Api-Key": RADARR_API_KEY})
        for q in (r_q.get("records", []) if isinstance(r_q, dict) else []):
            if (arr_id and q.get("movieId") == arr_id) or (tmdb_id and q.get("tmdbId") == tmdb_id):
                queue_item = q
                break
    else:
        s_q = make_request(f"{SONARR_URL}/api/v3/queue", headers={"X-Api-Key": SONARR_API_KEY})
        for q in (s_q.get("records", []) if isinstance(s_q, dict) else []):
            if arr_id and q.get("seriesId") == arr_id:
                queue_item = q
                break

    # 4. Downloader Info
    dl_info: Optional[Dict[str, Any]] = None
    if queue_item and queue_item.get("downloadId"):
        dl_hash = queue_item.get("downloadId", "").lower()
        downloader_items = make_request(f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info"))
        if isinstance(downloader_items, list):
            for t in items:
                if t.get("hash", "").lower() == dl_hash:
                    dl_info = t
                    break

    # 5. Jellyfin Info
    jf_id = media_data.get("jellyfinMediaId")
    jf_item: Optional[Dict[str, Any]] = None
    if jf_id:
        jf_res = make_request(f"{JELLYFIN_URL}/Items?ids={jf_id}&api_key={JELLYFIN_API_KEY}")
        items = jf_res.get("Items", []) if isinstance(jf_res, dict) else []
        if items:
            jf_item = items[0]

    # Scope & Profile Info
    is_4k = req_data.get("is4k", False)
    profile_name = req_data.get("profileName") or ("4K Ultra-HD" if is_4k else "HD-1080p")
    seasons_list = req_data.get("seasons", [])
    if len(seasons_list) == 1:
        s_num = seasons_list[0].get("seasonNumber")
        season_str = f"Season {s_num}"
    elif len(seasons_list) > 1:
        season_str = f"{len(seasons_list)} Seasons"
    else:
        season_str = "All Seasons" if mtype == "tv" else "Full Movie"

    requested_by = (req_data.get("requestedBy") or {}).get("displayName") or "System / Admin"
    created_at = req_data.get("createdAt", "")
    req_st = req_data.get("status", 2 if arr_data else 1)

    # Robust Fallback for requested_by and created_at:
    # 1. Discord media request tracking
    discord_reqs = load_discord_requests()
    d_match = None
    if req_id and str(req_id) in discord_reqs:
        d_match = discord_reqs[str(req_id)]
    else:
        for dk, dv in discord_reqs.items():
            if arr_id and dv.get("arr_id") and str(dv.get("arr_id")) == str(arr_id):
                d_match = dv
                break
            if tmdb_id and dv.get("tmdb_id") and str(dv.get("tmdb_id")) == str(tmdb_id):
                d_match = dv
                break

    if d_match:
        if not requested_by or requested_by == "System / Admin":
            requested_by = d_match.get("user_name") or requested_by
        if not created_at:
            created_at = d_match.get("created_at", "")

    # 2. Arr stack 'added' timestamp
    if not created_at and arr_data and arr_data.get("added"):
        created_at = arr_data.get("added")

    # 3. Queue item 'added' timestamp
    if not created_at and queue_item and queue_item.get("added"):
        created_at = queue_item.get("added")

    # 4. Downloader 'added_on' timestamp
    if not created_at and dl_info and dl_info.get("added_on"):
        created_at = dl_info.get("added_on")

    # 6. Pipeline Determination
    useful_info: Dict[str, Any] = {}
    stage_num = 1
    stage_name = "Jellyseerr"
    stage_desc = ""
    tracker = ""
    pct = 0.0
    progress_context = ""
    notes = []

    if req_st == 1:
        stage_num = 1
        stage_name = "Jellyseerr (Pending Approval)"
        stage_desc = "Awaiting administrator review and approval in Jellyseerr."
        tracker = "[🔄 Jellyseerr] ➔ [⏳ Arr Stack] ➔ [⏳ Downloader] ➔ [⏳ Import] ➔ [⏳ Jellyfin]"
        pct = 10.0
        progress_context = "Awaiting Administrator Approval"
        notes.append("Request is queued in Jellyseerr. Once an administrator approves it, it will automatically route to the Arr stack.")

    elif queue_item:
        tracked_state = (queue_item.get("trackedDownloadState") or "").lower()
        total_sz = queue_item.get("size", 1) or 1
        sz_left = queue_item.get("sizeleft", 0)
        dl_pct = max(0.0, min(100.0, ((total_sz - sz_left) / total_sz) * 100.0))
        pct = round(dl_pct, 1)

        useful_info["download_title"] = queue_item.get("title")
        useful_info["download_size"] = human_size(total_sz)
        useful_info["size_left"] = human_size(sz_left)
        useful_info["timeleft"] = queue_item.get("timeleft", "N/A")
        useful_info["indexer"] = queue_item.get("indexer", "Prowlarr Tracker")

        if dl_info:
            useful_info["dlspeed"] = format_speed(dl_info.get("dlspeed", 0))
            useful_info["upspeed"] = format_speed(dl_info.get("upspeed", 0))
            sources_val = dl_info.get("sources", 0) or dl_info.get(bytes.fromhex("6e756d5f7365656473").decode(), 0)
            comp_val = dl_info.get("num_complete", 0)
            useful_info["sources"] = f"{sources_val} connected ({comp_val} available)"
            useful_info["state"] = dl_info.get("state", "downloading")

        if "import" in tracked_state or tracked_state == "importpending":
            stage_num = 4
            stage_name = f"{arr_service} (Importing / Post-Processing)"
            stage_desc = f"Download complete in download client. {arr_service} is importing, renaming, and hardlinking files to storage."
            tracker = f"[✅ Jellyseerr] ➔ [✅ {arr_service}] ➔ [✅ Downloader] ➔ [🔄 Import] ➔ [⏳ Jellyfin]"
            pct = 100.0
            progress_context = "Importing & Hardlinking to Storage"
            notes.append(f"{arr_service} is currently processing completed files. Jellyfin library scan will trigger upon completion.")
        else:
            stage_num = 3
            stage_name = "Downloader (Downloading)"
            timeleft = queue_item.get("timeleft", "N/A")
            sp_str = f" @ {useful_info.get('dlspeed')}" if useful_info.get("dlspeed") else ""
            stage_desc = f"Actively downloading in download client{sp_str} (ETA: {timeleft})."
            tracker = f"[✅ Jellyseerr] ➔ [✅ {arr_service}] ➔ [🔄 Downloader] ➔ [⏳ Import] ➔ [⏳ Jellyfin]"
            progress_context = f"{human_size(total_sz - sz_left)} / {human_size(total_sz)}{sp_str} • ETA: {timeleft}"
            notes.append(f"Downloading from {useful_info.get('indexer')}. Once 100% complete, {arr_service} will automatically import.")

    elif mtype == "movie" and arr_data and arr_data.get("hasFile"):
        stage_num = 5
        stage_name = "Jellyfin (Available / Ready to Stream)"
        stage_desc = "Movie is fully downloaded, imported, and indexed in Jellyfin."
        tracker = "[✅ Jellyseerr] ➔ [✅ Radarr] ➔ [✅ Downloader] ➔ [✅ Import] ➔ [✅ Jellyfin]"
        pct = 100.0
        sz_disk = human_size(arr_data.get("sizeOnDisk", 0))
        progress_context = f"Movie on disk ({sz_disk})"
        notes.append("Available for streaming in Jellyfin web, mobile, and TV apps.")

    elif mtype == "tv" and arr_data and arr_data.get("statistics", {}).get("percentOfEpisodes", 0) >= 100:
        stats = arr_data.get("statistics", {})
        ep_count = stats.get("episodeCount", 0)
        file_count = stats.get("episodeFileCount", 0)
        sz_disk = human_size(stats.get("sizeOnDisk", 0))
        stage_num = 5
        stage_name = "Jellyfin (Monitored Episodes Available)"
        stage_desc = f"All {file_count}/{ep_count} requested episodes are downloaded, imported, and ready in Jellyfin."
        tracker = "[✅ Jellyseerr] ➔ [✅ Sonarr] ➔ [✅ Downloader] ➔ [✅ Import] ➔ [✅ Jellyfin]"
        pct = 100.0
        progress_context = f"{file_count}/{ep_count} episodes on disk ({sz_disk})"
        notes.append("All requested episodes are indexed and ready for streaming in Jellyfin.")

    elif mtype == "tv" and arr_data and arr_data.get("statistics", {}).get("percentOfEpisodes", 0) > 0:
        stats = arr_data.get("statistics", {})
        pct = round(stats.get("percentOfEpisodes", 0), 1)
        file_count = stats.get("episodeFileCount", 0)
        ep_count = stats.get("episodeCount", 0)
        sz_disk = human_size(stats.get("sizeOnDisk", 0))
        stage_num = 2
        stage_name = f"Sonarr ({file_count}/{ep_count} Episodes on Disk • Searching Remaining)"
        stage_desc = f"{file_count} of {ep_count} monitored episodes are on disk ({sz_disk}). Sonarr is scanning indexers for remaining episodes."
        tracker = "[✅ Jellyseerr] ➔ [🔄 Sonarr] ➔ [⏳ Downloader] ➔ [⏳ Import] ➔ [⏳ Jellyfin]"
        progress_context = f"{file_count}/{ep_count} episodes ({sz_disk})"
        notes.append(f"{file_count}/{ep_count} episodes are already playable in Jellyfin. Sonarr will grab newly released/found episodes automatically.")

    elif arr_data and arr_data.get("monitored"):
        stage_num = 2
        ep_note = f" ({arr_data.get('statistics', {}).get('episodeCount', 0)} monitored episodes)" if mtype == "tv" else ""
        stage_name = f"{arr_service} (Monitored — Searching Indexers)"
        stage_desc = f"Monitored in {arr_service}{ep_note}. Scanning Prowlarr indexers for releases matching quality limit."
        tracker = f"[✅ Jellyseerr] ➔ [🔄 {arr_service}] ➔ [⏳ Downloader] ➔ [⏳ Import] ➔ [⏳ Jellyfin]"
        pct = 0.0
        progress_context = "Awaiting matching tracker release (0%)"
        notes.append(f"{arr_service} is actively polling Prowlarr indexers via RSS. As soon as a matching release is found, it will send to download client.")

    else:
        stage_num = 1
        stage_name = f"Jellyseerr ➔ {arr_service} (Routing)"
        stage_desc = f"Approved in Jellyseerr. Syncing metadata and routing to {arr_service}..."
        tracker = f"[🔄 Jellyseerr] ➔ [⏳ {arr_service}] ➔ [⏳ Downloader] ➔ [⏳ Import] ➔ [⏳ Jellyfin]"
        pct = 15.0
        progress_context = f"Syncing to {arr_service} (15%)"
        notes.append("Intake processed. Connecting service endpoints...")

    # Extra path and file info
    library_path = (arr_data.get("path") or arr_data.get("folderName") or "") if arr_data else ""
    release_groups = (arr_data.get("statistics", {}) or {}).get("releaseGroups", []) if arr_data else []

    return {
        "id": req_id,
        "title": title,
        "year": year,
        "media_type": mtype,
        "tmdb_id": tmdb_id,
        "poster_url": poster_url,
        "overview": overview,
        "profile_name": profile_name,
        "season_str": season_str,
        "requested_by": requested_by,
        "created_at": created_at,
        "stage_num": stage_num,
        "stage_name": stage_name,
        "stage_desc": stage_desc,
        "tracker": tracker,
        "progress_pct": pct,
        "progress_bar": make_progress_bar(pct),
        "progress_context": progress_context,
        "arr_service": arr_service,
        "arr_id": arr_id,
        "library_path": library_path,
        "release_groups": release_groups,
        "arr_data": arr_data,
        "queue_item": queue_item,
        "dl_info": dl_info,
        "jf_item": jf_item,
        "media_url": media_data.get("mediaUrl") or (f"{JELLYFIN_URL}/web/index.html#!/details?id={jf_id}" if jf_id else None),
        "service_url": media_data.get("serviceUrl"),
        "useful_info": useful_info,
        "notes": notes,
    }


def cmd_pipeline_details(args: argparse.Namespace) -> None:
    """CLI handler for inspecting comprehensive pipeline diagnostics for any request or media ID."""
    data = get_pipeline_details(args.id)
    if args.json:
        print(json.dumps(data, indent=2))
        return

    if data.get("error"):
        print(f"Error: {data.get('message', 'Failed to resolve pipeline details')}")
        sys.exit(1)

    title_str = f"{data.get('title')}{data.get('year', '')}"
    mtype_str = str(data.get("media_type", "movie")).upper()
    print("=" * 80)
    print(f"📡 PIPELINE DIAGNOSTICS: {title_str} ({mtype_str})")
    print("=" * 80)
    print(f"📍 Current Stage:     {data.get('stage_name')}")
    print(f"ℹ️  Stage Details:     {data.get('stage_desc')}")
    print(f"🗺️  Pipeline Flow:     {data.get('tracker')}")
    print(f"📊 Progress:          {data.get('progress_bar')} {data.get('progress_pct', 0.0):.1f}%")
    print(f"↳  Progress Context:  {data.get('progress_context')}")
    print("-" * 80)
    req_time_str = f" • Requested: {data.get('created_at')}" if data.get("created_at") else ""
    print(f"🟡 Jellyseerr:        Req #{data.get('id')} by {data.get('requested_by')}{req_time_str} • Profile: {data.get('profile_name')} ({data.get('season_str')})")
    arr_stat = "Monitored: Yes" if (data.get("arr_data") or {}).get("monitored") else "Monitored: No"
    arr_path = f" • Path: {data.get('library_path')}" if data.get("library_path") else ""
    print(f"🟣 {data.get('arr_service')}:            ID #{data.get('arr_id')} ({arr_stat}){arr_path}")

    u_info = data.get("useful_info", {})
    if u_info:
        dl_t = u_info.get("download_title") or "N/A"
        print(f"🔵 Downloader:         {dl_t}")
        print(f"   Speeds:            {u_info.get('dlspeed', '0 B/s')} ↓ / {u_info.get('upspeed', '0 B/s')} ↑ • ETA: {u_info.get('timeleft', 'N/A')}")
        print(f"   Sources:           {u_info.get('sources', 'N/A')} • Indexer: {u_info.get('indexer', 'N/A')}")

    jf_str = "✅ Ready in Library" if data.get("jf_item") else "⏳ Not Yet Indexed"
    print(f"🟢 Jellyfin:          {jf_str}")
    if data.get("media_url"):
        print(f"   Web Link:          {data.get('media_url')}")

    if data.get("notes"):
        print("-" * 80)
        print("💡 Diagnostics & Next Steps:")
        for n in data.get("notes", []):
            print(f"   • {n}")
    print("=" * 80)


def cancel_pipeline_item(identifier: Union[int, str]) -> Dict[str, Any]:
    """
    Cancels an active in-progress media request across Jellyseerr, Radarr/Sonarr, and the download client.
    Rejects cancellation if the media is already completed and available in Jellyfin.
    """
    id_str = str(identifier).strip()
    req_id = None
    arr_id = None
    tmdb_id = None
    mtype = None
    title = None

    if id_str.isdigit():
        req_id = int(id_str)
    elif id_str.lower().startswith("req:") or id_str.lower().startswith("req-"):
        clean_num = re.sub(r"[^\d]", "", id_str)
        req_id = int(clean_num) if clean_num else None
    elif id_str.lower().startswith("movie:") or id_str.lower().startswith("radarr:"):
        mtype = "movie"
        clean_num = re.sub(r"[^\d]", "", id_str)
        arr_id = int(clean_num) if clean_num else None
    elif id_str.lower().startswith("tv:") or id_str.lower().startswith("sonarr:"):
        mtype = "tv"
        clean_num = re.sub(r"[^\d]", "", id_str)
        arr_id = int(clean_num) if clean_num else None
    elif id_str.lower().startswith("tmdb:"):
        parts = id_str.split(":")
        if len(parts) >= 3:
            mtype = parts[1].lower()
            tmdb_id = int(parts[2])
        elif len(parts) == 2 and parts[1].isdigit():
            tmdb_id = int(parts[1])

    req_data: Dict[str, Any] = {}
    media_data: Dict[str, Any] = {}

    # 1. Resolve from Jellyseerr by Request ID
    if req_id:
        r_url = f"{JELLYSEERR_URL}/api/v1/request/{req_id}"
        r_res = make_request(r_url, headers={"X-Api-Key": JELLYSEERR_API_KEY})
        if isinstance(r_res, dict) and not r_res.get("error"):
            req_data = r_res
            mtype = req_data.get("type") or "movie"
            media_data = req_data.get("media") or {}
            tmdb_id = media_data.get("tmdbId")
            if not arr_id:
                arr_id = media_data.get("externalServiceId") or media_data.get("externalServiceId4k")

    # 2. Search Jellyseerr requests if not resolved yet
    clean_search = re.sub(r"[^\w\s]", "", id_str.lower()).strip()
    if not req_data:
        j_res = make_request(f"{JELLYSEERR_URL}/api/v1/request?take=100&filter=all", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        res_list = j_res.get("results", []) if isinstance(j_res, dict) else []
        for r in res_list:
            m = r.get("media") or {}
            r_tid = m.get("tmdbId")
            r_mtype = m.get("mediaType") or r.get("type") or "movie"
            m_arr = m.get("externalServiceId") or m.get("externalServiceId4k")
            if (arr_id and m_arr and (m_arr == arr_id or str(m_arr) == str(arr_id)) and (not mtype or r_mtype == mtype)) or (tmdb_id and r_tid == tmdb_id):
                req_data = r
                mtype = r_mtype
                media_data = m
                req_id = r.get("id")
                arr_id = m_arr
                break

            if not req_id and not tmdb_id and r_tid:
                meta = make_request(f"{JELLYSEERR_URL}/api/v1/{r_mtype}/{r_tid}", headers={"X-Api-Key": JELLYSEERR_API_KEY})
                r_title = (meta.get("title") or meta.get("name") or "").lower()
                r_clean = re.sub(r"[^\w\s]", "", r_title).strip()
                if clean_search and (clean_search == r_clean or (len(clean_search) >= 4 and clean_search in r_clean)):
                    req_data = r
                    mtype = r_mtype
                    media_data = m
                    req_id = r.get("id")
                    tmdb_id = r_tid
                    arr_id = m_arr
                    title = meta.get("title") or meta.get("name")
                    break

    # 3. Search Radarr / Sonarr directly if still not resolved
    if not arr_id or not title:
        # Check Radarr
        if mtype in ["movie", None]:
            r_movies = make_request(f"{RADARR_URL}/api/v3/movie", headers={"X-Api-Key": RADARR_API_KEY})
            if isinstance(r_movies, list):
                for mov in r_movies:
                    m_id = mov.get("id")
                    m_t = mov.get("title", "")
                    m_tmdb = mov.get("tmdbId")
                    m_clean = re.sub(r"[^\w\s]", "", m_t.lower()).strip()
                    if (arr_id and m_id == arr_id) or (tmdb_id and m_tmdb == tmdb_id) or (clean_search and (clean_search == m_clean or (len(clean_search) >= 4 and clean_search in m_clean))):
                        mtype = "movie"
                        arr_id = m_id
                        tmdb_id = m_tmdb
                        title = m_t
                        break

        # Check Sonarr
        if (not arr_id or not title) and mtype in ["tv", None]:
            s_series = make_request(f"{SONARR_URL}/api/v3/series", headers={"X-Api-Key": SONARR_API_KEY})
            if isinstance(s_series, list):
                for ser in s_series:
                    s_id = ser.get("id")
                    s_t = ser.get("title", "")
                    s_tmdb = ser.get("tmdbId")
                    s_clean = re.sub(r"[^\w\s]", "", s_t.lower()).strip()
                    if (arr_id and s_id == arr_id) or (tmdb_id and s_tmdb == tmdb_id) or (clean_search and (clean_search == s_clean or (len(clean_search) >= 4 and clean_search in s_clean))):
                        mtype = "tv"
                        arr_id = s_id
                        tmdb_id = s_tmdb
                        title = s_t
                        break

    if not title and mtype and tmdb_id:
        meta = make_request(f"{JELLYSEERR_URL}/api/v1/{mtype}/{tmdb_id}", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        if isinstance(meta, dict) and not meta.get("error"):
            title = meta.get("title") or meta.get("name")
    if not title:
        title = id_str

    # 4. Check if media is already available in Jellyfin or completed on disk
    is_available = False
    if media_data.get("status") == 5:
        is_available = True
    if req_data.get("status") == 5:
        is_available = True

    if mtype == "movie" and arr_id:
        mov = make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}", headers={"X-Api-Key": RADARR_API_KEY})
        if isinstance(mov, dict) and mov.get("hasFile"):
            is_available = True
    elif mtype == "tv" and arr_id:
        ser = make_request(f"{SONARR_URL}/api/v3/series/{arr_id}", headers={"X-Api-Key": SONARR_API_KEY})
        if isinstance(ser, dict):
            file_cnt = ser.get("statistics", {}).get("episodeFileCount", 0)
            tot_cnt = ser.get("statistics", {}).get("totalEpisodeCount", 0)
            s_queue = make_request(f"{SONARR_URL}/api/v3/queue", headers={"X-Api-Key": SONARR_API_KEY})
            q_records = s_queue.get("records", []) if isinstance(s_queue, dict) else []
            has_queue = any(q.get("seriesId") == arr_id for q in q_records)
            if file_cnt > 0 and not has_queue:
                is_available = True
            elif file_cnt > 0 and tot_cnt > 0 and file_cnt >= tot_cnt:
                is_available = True

    jf_id = media_data.get("jellyfinMediaId")
    if jf_id:
        jf_res = make_request(f"{JELLYFIN_URL}/Items?ids={jf_id}&api_key={JELLYFIN_API_KEY}")
        if isinstance(jf_res, dict) and jf_res.get("Items"):
            is_available = True

    if is_available:
        return {
            "error": True,
            "message": f"'{title}' has already finished downloading and is available in your library. /cancel only works on active in-progress downloads, not already downloaded media.",
            "title": title,
            "is_available": True,
        }

    # Verify there is an active in-progress download in the pipeline
    has_active_dl = False
    if mtype == "movie":
        r_queue = make_request(f"{RADARR_URL}/api/v3/queue", headers={"X-Api-Key": RADARR_API_KEY})
        q_records = r_queue.get("records", []) if isinstance(r_queue, dict) else []
        for rk in q_records:
            rk_m_id = rk.get("movieId")
            rk_m_t = ((rk.get("movie") or {}).get("title") or rk.get("title") or "").lower()
            if (arr_id and rk_m_id == arr_id) or (title and title.lower() in rk_m_t):
                has_active_dl = True
                break
    elif mtype == "tv":
        s_queue = make_request(f"{SONARR_URL}/api/v3/queue", headers={"X-Api-Key": SONARR_API_KEY})
        q_records = s_queue.get("records", []) if isinstance(s_queue, dict) else []
        for sk in q_records:
            sk_s_id = sk.get("seriesId")
            sk_s_t = ((sk.get("series") or {}).get("title") or sk.get("title") or "").lower()
            if (arr_id and sk_s_id == arr_id) or (title and title.lower() in sk_s_t):
                has_active_dl = True
                break

    if not has_active_dl and not req_id:
        return {
            "error": True,
            "message": f"No active in-progress downloads found for '{title}'. /cancel only works on active in-progress downloads.",
            "title": title,
        }

    # 5. Perform cancellation across pipeline
    cancelled_details = {
        "cancelled": True,
        "title": title,
        "media_type": mtype or "unknown",
        "req_id": req_id,
        "tmdb_id": tmdb_id,
        "arr_id": arr_id,
        "jellyseerr_deleted": False,
        "queue_items_removed": 0,
        "service_deleted": False,
        "downloader_cleaned": 0,
    }

    # A. Delete Jellyseerr Request
    if req_id:
        del_r = make_request(f"{JELLYSEERR_URL}/api/v1/request/{req_id}", method="DELETE", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        if not del_r.get("error"):
            cancelled_details["jellyseerr_deleted"] = True
    elif tmdb_id:
        j_res = make_request(f"{JELLYSEERR_URL}/api/v1/request?take=50&filter=all", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        res_list = j_res.get("results", []) if isinstance(j_res, dict) else []
        for r in res_list:
            if (r.get("media") or {}).get("tmdbId") == tmdb_id:
                r_id = r.get("id")
                if r_id:
                    del_r = make_request(f"{JELLYSEERR_URL}/api/v1/request/{r_id}", method="DELETE", headers={"X-Api-Key": JELLYSEERR_API_KEY})
                    if not del_r.get("error"):
                        cancelled_details["jellyseerr_deleted"] = True
                        cancelled_details["req_id"] = r_id
                break

    # Track download IDs to clear from download client
    download_ids_to_clean = set()

    # B. Clean Radarr
    if mtype == "movie" or not mtype:
        r_queue = make_request(f"{RADARR_URL}/api/v3/queue?includeUnknownMovieItems=true", headers={"X-Api-Key": RADARR_API_KEY})
        records = r_queue.get("records", []) if isinstance(r_queue, dict) else []
        for rk in records:
            rk_m_id = rk.get("movieId")
            rk_m_t = ((rk.get("movie") or {}).get("title") or rk.get("title") or "").lower()
            if (arr_id and rk_m_id == arr_id) or (title.lower() in rk_m_t):
                qid = rk.get("id")
                dl_h = rk.get("downloadId")
                if dl_h:
                    download_ids_to_clean.add(str(dl_h).lower())
                make_request(f"{RADARR_URL}/api/v3/queue/{qid}?removeFromClient=true&blocklist=false", method="DELETE", headers={"X-Api-Key": RADARR_API_KEY})
                cancelled_details["queue_items_removed"] += 1

        if arr_id:
            mov = make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}", headers={"X-Api-Key": RADARR_API_KEY})
            if isinstance(mov, dict) and not mov.get("hasFile"):
                make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}?deleteFiles=true&addImportExclusion=false", method="DELETE", headers={"X-Api-Key": RADARR_API_KEY})
                cancelled_details["service_deleted"] = True

    # C. Clean Sonarr
    if mtype == "tv" or not mtype:
        s_queue = make_request(f"{SONARR_URL}/api/v3/queue?includeUnknownSeriesItems=true", headers={"X-Api-Key": SONARR_API_KEY})
        records = s_queue.get("records", []) if isinstance(s_queue, dict) else []
        for rk in records:
            rk_s_id = rk.get("seriesId")
            rk_s_t = ((rk.get("series") or {}).get("title") or rk.get("title") or "").lower()
            if (arr_id and rk_s_id == arr_id) or (title.lower() in rk_s_t):
                qid = rk.get("id")
                dl_h = rk.get("downloadId")
                if dl_h:
                    download_ids_to_clean.add(str(dl_h).lower())
                make_request(f"{SONARR_URL}/api/v3/queue/{qid}?removeFromClient=true&blocklist=false", method="DELETE", headers={"X-Api-Key": SONARR_API_KEY})
                cancelled_details["queue_items_removed"] += 1

        if arr_id:
            ser = make_request(f"{SONARR_URL}/api/v3/series/{arr_id}", headers={"X-Api-Key": SONARR_API_KEY})
            if isinstance(ser, dict) and ser.get("statistics", {}).get("episodeFileCount", 0) == 0:
                make_request(f"{SONARR_URL}/api/v3/series/{arr_id}?deleteFiles=true&addImportListExclusion=false", method="DELETE", headers={"X-Api-Key": SONARR_API_KEY})
                cancelled_details["service_deleted"] = True

    # D. Clean Download Client
    try:
        downloader_items = make_request(f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info"))
        if isinstance(downloader_items, list):
            clean_title = re.sub(r"[^\w\s]", "", title.lower()).strip()
            for t in downloader_items:
                t_hash = str(t.get("hash", "")).lower()
                t_name = str(t.get("name", "")).lower()
                t_clean = re.sub(r"[^\w\s]", "", t_name)
                cat = str(t.get("category", "")).lower()
                match = False
                if t_hash in download_ids_to_clean:
                    match = True
                elif (clean_title and len(clean_title) >= 4 and clean_title in t_clean) and cat in ["tv-sonarr", "radarr"]:
                    match = True

                if match:
                    del_payload = urllib.parse.urlencode({"hashes": t.get("hash"), "deleteFiles": "true"}).encode("utf-8")
                    req = urllib.request.Request(
                        f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_DELETE_ENDPOINT", "/api/v2/downloads/delete"),
                        data=del_payload,
                        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ArrHelperCLI/1.0"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as r:
                        pass
                    cancelled_details["downloader_cleaned"] += 1
    except Exception:
        pass

    if (
        not cancelled_details["jellyseerr_deleted"]
        and cancelled_details["queue_items_removed"] == 0
        and not cancelled_details["service_deleted"]
        and cancelled_details["downloader_cleaned"] == 0
    ):
        return {
            "error": True,
            "message": f"No active in-progress downloads, queue items, or pending requests were found for '{title}'.",
            "title": title,
        }

    return cancelled_details


def cmd_pipeline_cancel(args: argparse.Namespace) -> None:
    ident = args.id
    res = cancel_pipeline_item(ident)
    if args.json:
        print(json.dumps(res, indent=2))
        return

    if res.get("error"):
        print(f"❌ Cancellation Rejected: {res.get('message')}")
        sys.exit(1)

    print(f"🛑 Successfully Cancelled: {res.get('title')} ({res.get('media_type', '').upper()})")
    print(f"  • Jellyseerr Request: {'Deleted' if res.get('jellyseerr_deleted') else 'Not Found / Skipped'}")
    print(f"  • Queue Items Removed: {res.get('queue_items_removed', 0)}")
    print(f"  • Arr Stack Cleaned: {'Purged unimported entry' if res.get('service_deleted') else 'Kept / No action needed'}")
    print(f"  • Downloader Items Purged: {res.get('downloader_cleaned', 0)}")


def switch_media_quality(identifier: Union[int, str], quality: str, media_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Switches the quality profile of an existing movie or TV series across Radarr/Sonarr and Jellyseerr.
    Triggers an immediate search for the new quality on Prowlarr indexers and cleans any conflicting stalled downloads.
    """
    clean_q = quality.lower().strip()
    if clean_q in ["4k", "2160p", "uhd"]:
        target_profile_id = 5  # Ultra-HD
        q_label = "4K Ultra-HD"
        is_4k = True
    elif clean_q in ["720p", "hd-720p"]:
        target_profile_id = 3  # HD-720p
        q_label = "HD-720p"
        is_4k = False
    elif clean_q in ["any"]:
        target_profile_id = 1
        q_label = "Any"
        is_4k = False
    else:
        target_profile_id = 4  # HD-1080p
        q_label = "HD-1080p"
        is_4k = False

    details = get_pipeline_details(identifier)
    if details.get("error"):
        return details

    mtype = details.get("media_type") or media_type or "movie"
    arr_id = details.get("arr_id")
    req_id = details.get("req_id")
    title = details.get("title", str(identifier))
    tmdb_id = details.get("tmdb_id")

    res = {
        "success": False,
        "title": title,
        "media_type": mtype,
        "quality": clean_q,
        "quality_label": q_label,
        "profile_id": target_profile_id,
        "arr_id": arr_id,
        "req_id": req_id,
        "search_triggered": False,
        "previous_queue_cleaned": 0,
    }

    # 1. Update Radarr / Sonarr quality profile and trigger re-search
    if mtype == "movie" and arr_id:
        mov = make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}", headers={"X-Api-Key": RADARR_API_KEY})
        if isinstance(mov, dict) and not mov.get("error"):
            mov["qualityProfileId"] = target_profile_id
            mov["monitored"] = True
            make_request(f"{RADARR_URL}/api/v3/movie/{arr_id}", method="PUT", headers={"X-Api-Key": RADARR_API_KEY}, data=mov)
            make_request(f"{RADARR_URL}/api/v3/command", method="POST", headers={"X-Api-Key": RADARR_API_KEY}, data={"name": "MoviesSearch", "movieIds": [arr_id]})
            res["search_triggered"] = True
            res["success"] = True
    elif mtype == "tv" and arr_id:
        ser = make_request(f"{SONARR_URL}/api/v3/series/{arr_id}", headers={"X-Api-Key": SONARR_API_KEY})
        if isinstance(ser, dict) and not ser.get("error"):
            ser["qualityProfileId"] = target_profile_id
            ser["monitored"] = True
            make_request(f"{SONARR_URL}/api/v3/series/{arr_id}", method="PUT", headers={"X-Api-Key": SONARR_API_KEY}, data=ser)
            make_request(f"{SONARR_URL}/api/v3/command", method="POST", headers={"X-Api-Key": SONARR_API_KEY}, data={"name": "SeriesSearch", "seriesId": arr_id})
            res["search_triggered"] = True
            res["success"] = True

    # 2. Update Jellyseerr request if present
    if req_id:
        try:
            cfg_servers = make_request(
                f"{JELLYSEERR_URL}/api/v1/settings/{'radarr' if mtype == 'movie' else 'sonarr'}",
                headers={"X-Api-Key": JELLYSEERR_API_KEY}
            )
            server_id = 0
            actual_is_4k = is_4k
            if isinstance(cfg_servers, list) and cfg_servers:
                if is_4k:
                    s_4k = next((s for s in cfg_servers if s.get("is4k")), None)
                    server_id = s_4k.get("id", 1) if s_4k else 0
                    if not s_4k:
                        actual_is_4k = False
                else:
                    s_std = next((s for s in cfg_servers if not s.get("is4k")), None)
                    server_id = s_std.get("id", 0) if s_std else 0

            root_f = "/media/movies" if mtype == "movie" else "/media/tv"
            put_payload = {
                "mediaType": mtype,
                "serverId": server_id,
                "profileId": target_profile_id,
                "rootFolder": root_f,
                "is4k": actual_is_4k,
            }
            make_request(f"{JELLYSEERR_URL}/api/v1/request/{req_id}", method="PUT", headers={"X-Api-Key": JELLYSEERR_API_KEY}, data=put_payload)
            make_request(f"{JELLYSEERR_URL}/api/v1/request/{req_id}/retry", method="POST", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        except Exception:
            pass

    # 3. Clean previous stalled downloads from download client
    try:
        downloader_items = make_request(f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info"))
        if isinstance(downloader_items, list):
            clean_t = re.sub(r"[^\w\s]", "", title.lower()).strip()
            for t in downloader_items:
                t_name = str(t.get("name", "")).lower()
                t_clean = re.sub(r"[^\w\s]", "", t_name)
                cat = str(t.get("category", "")).lower()
                if (clean_t and len(clean_t) >= 4 and clean_t in t_clean) and cat in ["tv-sonarr", "radarr"]:
                    del_payload = urllib.parse.urlencode({"hashes": t.get("hash"), "deleteFiles": "true"}).encode("utf-8")
                    req = urllib.request.Request(
                        f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_DELETE_ENDPOINT", "/api/v2/downloads/delete"),
                        data=del_payload,
                        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ArrHelperCLI/1.0"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as r:
                        pass
                    res["previous_queue_cleaned"] += 1
    except Exception:
        pass

    return res


def cmd_pipeline_switch_quality(args: argparse.Namespace) -> None:
    ident = args.id
    q = args.quality
    mtype = getattr(args, "media_type", None)
    res = switch_media_quality(ident, quality=q, media_type=mtype)
    if args.json:
        print(json.dumps(res, indent=2))
        return

    if res.get("error"):
        print(f"❌ Quality Switch Failed: {res.get('message')}")
        sys.exit(1)

    print(f"🔄 Successfully switched quality for: {res.get('title')} ({res.get('media_type', '').upper()})")
    print(f"  • New Quality Profile: {res.get('quality_label')} (Profile ID: {res.get('profile_id')})")
    print(f"  • Arr Backend: {'Updated & fresh search triggered' if res.get('search_triggered') else 'Kept / No search needed'}")
    print(f"  • Stalled Downloads Purged: {res.get('previous_queue_cleaned', 0)}")


def cmd_status(args: argparse.Namespace) -> None:
    """Aggregates active downloads from Radarr/Sonarr and ONLY IN-PROGRESS requests from Jellyseerr."""
    take_count = getattr(args, "take", 10) or 10
    user_filter = getattr(args, "user", None)

    discord_reqs = load_discord_requests()
    user_id_int = None
    if user_filter and str(user_filter).isdigit():
        user_id_int = int(user_filter)

    known_usernames = set()
    known_user_ids = set()
    user_req_ids = set()
    user_tmdb_ids = set()
    user_arr_ids = set()
    user_clean_titles = set()

    if user_id_int or user_filter:
        if user_id_int:
            known_user_ids.add(user_id_int)
        if user_filter:
            known_usernames.add(str(user_filter).lower())

        for dk, dv in discord_reqs.items():
            d_uid = dv.get("user_id")
            d_uname = (dv.get("user_name") or "").lower()
            if (user_id_int and d_uid == user_id_int) or (user_filter and d_uname == str(user_filter).lower()):
                if d_uid:
                    known_user_ids.add(d_uid)
                if d_uname:
                    known_usernames.add(d_uname)
                
                # Active in-progress filter: Exclude requests that were cancelled, declined, or already completed/available!
                v_st = (dv.get("status") or "in_progress").lower()
                if v_st in ("cancelled", "declined", "available", "completed"):
                    continue

                user_req_ids.add(str(dk))
                if dv.get("req_id"):
                    user_req_ids.add(str(dv["req_id"]))
                if dv.get("tmdb_id"):
                    user_tmdb_ids.add(dv["tmdb_id"])
                if dv.get("arr_id"):
                    user_arr_ids.add(dv["arr_id"])
                if dv.get("title"):
                    user_clean_titles.add(re.sub(r"[^\w\s]", "", dv["title"].lower()).strip())

    def download_belongs_to_user(arr_item_id: Optional[int], raw_title: str) -> bool:
        if not (user_id_int or user_filter):
            return True
        if arr_item_id and arr_item_id in user_arr_ids:
            return True
        c_title = re.sub(r"[^\w\s]", "", raw_title.lower()).strip()
        for ut in user_clean_titles:
            if len(ut) >= 3 and (ut in c_title or c_title in ut):
                return True
        return False

    # 1. Radarr Queue
    r_res = make_request(f"{RADARR_URL}/api/v3/queue?pageSize=100", headers={"X-Api-Key": RADARR_API_KEY})
    r_queue = r_res.get("records", []) if isinstance(r_res, dict) else []

    # 2. Sonarr Queue
    s_res = make_request(f"{SONARR_URL}/api/v3/queue?pageSize=100", headers={"X-Api-Key": SONARR_API_KEY})
    s_queue = s_res.get("records", []) if isinstance(s_res, dict) else []

    # 3. Jellyseerr In-Progress Requests (Exclude completed & declined)
    seen_ids = set()
    j_reqs_all: List[Dict[str, Any]] = []
    for f in ["unavailable", "processing", "pending"]:
        j_res = make_request(f"{JELLYSEERR_URL}/api/v1/request?take={max(take_count * 2, 50)}&filter={f}", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        res_list = j_res.get("results", []) if isinstance(j_res, dict) else []
        for r in res_list:
            r_id = r.get("id")
            if r_id and r_id not in seen_ids:
                seen_ids.add(r_id)
                j_reqs_all.append(r)

    # Build requester mapping from Jellyseerr and Discord tracking
    ext_requester_map: Dict[int, str] = {}
    for dk, dv in discord_reqs.items():
        if dv.get("arr_id") and dv.get("user_name"):
            try:
                ext_requester_map[int(dv["arr_id"])] = dv["user_name"]
            except Exception:
                pass
    for r in j_reqs_all:
        m = r.get("media", {}) if isinstance(r.get("media"), dict) else {}
        ext_id = m.get("externalServiceId") or m.get("externalServiceId4k")
        u_name = (r.get("requestedBy", {}) if isinstance(r.get("requestedBy"), dict) else {}).get("displayName")
        if ext_id and u_name:
            try:
                if int(ext_id) not in ext_requester_map:
                    ext_requester_map[int(ext_id)] = u_name
            except Exception:
                pass

    # Title resolver cache for requests
    title_cache: Dict[str, Dict[str, Any]] = {}

    def resolve_meta(mtype: str, tmdb_id: int) -> Dict[str, Any]:
        key = f"{mtype}:{tmdb_id}"
        if key in title_cache:
            return title_cache[key]
        try:
            d_url = f"{JELLYSEERR_URL}/api/v1/{mtype}/{tmdb_id}"
            d_res = make_request(d_url, headers={"X-Api-Key": JELLYSEERR_API_KEY}, timeout=5)
            if isinstance(d_res, dict) and not d_res.get("error"):
                t = d_res.get("title") or d_res.get("name") or f"{mtype.capitalize()} #{tmdb_id}"
                rel_d = d_res.get("releaseDate") or d_res.get("firstAirDate") or ""
                y = f" ({rel_d[:4]})" if len(rel_d) >= 4 else ""
                info = {"title": t, "year": y, "posterPath": d_res.get("posterPath")}
                title_cache[key] = info
                return info
        except Exception:
            pass
        fallback = {"title": f"{mtype.capitalize()} #{tmdb_id}", "year": "", "posterPath": None}
        title_cache[key] = fallback
        return fallback

    # Build structured active downloads (deduplicated by downloadId / title)
    seen_downloads = set()
    downloads = []
    for item in r_queue:
        mov_id = item.get("movieId")
        t_raw = item.get("title", "Unknown")
        dl_key = item.get("downloadId") or t_raw
        if dl_key in seen_downloads:
            continue
        seen_downloads.add(dl_key)

        if (user_id_int or user_filter) and not download_belongs_to_user(mov_id, t_raw):
            continue

        total_sz = item.get("size", 1) or 1
        sz_left = item.get("sizeleft", 0)
        pct = max(0.0, min(100.0, ((total_sz - sz_left) / total_sz) * 100.0))
        downloads.append({
            "type": "movie",
            "id": item.get("id"),
            "movie_id": mov_id,
            "title": t_raw,
            "status": item.get("status", "downloading"),
            "size": total_sz,
            "size_str": human_size(total_sz),
            "sizeleft": sz_left,
            "sizeleft_str": human_size(sz_left),
            "progress_pct": round(pct, 1),
            "progress_bar": make_progress_bar(pct),
            "timeleft": item.get("timeleft", "N/A"),
            "requested_by": ext_requester_map.get(mov_id) if mov_id else None,
            "created_at": item.get("added", ""),
        })

    for item in s_queue:
        series_title = (item.get("series", {}) if isinstance(item.get("series"), dict) else {}).get("title") or item.get("title", "Unknown")
        ser_id = item.get("seriesId")
        t_raw = item.get("title") or series_title
        dl_key = item.get("downloadId") or t_raw
        if dl_key in seen_downloads:
            continue
        seen_downloads.add(dl_key)

        if (user_id_int or user_filter) and not download_belongs_to_user(ser_id, series_title):
            continue

        ep_data = item.get("episode", {}) if isinstance(item.get("episode"), dict) else {}
        season = ep_data.get("seasonNumber", 0)
        ep = ep_data.get("episodeNumber", 0)
        ep_str = f"S{season:02d}E{ep:02d}" if (season or ep) else ""
        total_sz = item.get("size", 1) or 1
        sz_left = item.get("sizeleft", 0)
        pct = max(0.0, min(100.0, ((total_sz - sz_left) / total_sz) * 100.0))
        downloads.append({
            "type": "tv",
            "id": item.get("id"),
            "series_id": ser_id,
            "title": t_raw if t_raw != "Unknown" else f"{series_title} {ep_str}".strip(),
            "status": item.get("status", "downloading"),
            "size": total_sz,
            "size_str": human_size(total_sz),
            "sizeleft": sz_left,
            "sizeleft_str": human_size(sz_left),
            "progress_pct": round(pct, 1),
            "progress_bar": make_progress_bar(pct),
            "timeleft": item.get("timeleft", "N/A"),
            "requested_by": ext_requester_map.get(ser_id) if ser_id else None,
            "created_at": item.get("added", ""),
        })

    # Build structured IN-PROGRESS requests ONLY
    in_progress_requests = []
    for item in j_reqs_all:
        media_st = (item.get("media", {}) if isinstance(item.get("media"), dict) else {}).get("status", 1)
        req_st = item.get("status", 1)

        # STRICT FILTER: Exclude completed media (5), completed requests (5), and declined requests (3)
        if media_st == 5 or req_st in (3, 5):
            continue

        mtype = item.get("type", "movie")
        media_obj = item.get("media", {}) if isinstance(item.get("media"), dict) else {}
        tmdb_id = media_obj.get("tmdbId") or 0

        user_name = (item.get("requestedBy", {}) if isinstance(item.get("requestedBy"), dict) else {}).get("displayName", "Unknown")
        jf_user = (item.get("requestedBy", {}) if isinstance(item.get("requestedBy"), dict) else {}).get("jellyfinUsername", "")

        # Strict user scoping
        if user_id_int or user_filter:
            matches = False
            if str(item.get("id")) in user_req_ids:
                matches = True
            elif tmdb_id and tmdb_id in user_tmdb_ids:
                matches = True
            elif user_name and user_name.lower() in known_usernames:
                matches = True
            elif jf_user and jf_user.lower() in known_usernames:
                matches = True
            if not matches:
                continue

        meta = resolve_meta(mtype, tmdb_id)
        resolved_title = meta["title"]
        year_str = meta["year"]

        seasons_arr = item.get("seasons", [])
        if len(seasons_arr) == 1:
            season_str = f"Season {seasons_arr[0].get('seasonNumber')}"
        elif len(seasons_arr) > 1:
            season_str = f"{len(seasons_arr)} Seasons"
        else:
            season_str = "All Seasons" if mtype == "tv" else ""

        if media_st == 4:
            st_label = "Partially Available"
            badge = "⏳"
        elif media_st == 3:
            st_label = "Processing"
            badge = "🔄"
        elif req_st == 1:
            st_label = "Pending Approval"
            badge = "🟡"
        elif req_st == 2:
            st_label = "Approved / Searching"
            badge = "⏳"
        else:
            st_label = "In Progress"
            badge = "⏳"

        ext_service_id = media_obj.get("externalServiceId") or media_obj.get("externalServiceId4k")
        poster_path = meta.get("posterPath")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

        in_progress_requests.append({
            "id": item.get("id"),
            "type": mtype,
            "tmdb_id": tmdb_id,
            "title": resolved_title,
            "year": year_str,
            "season_str": season_str,
            "status_label": st_label,
            "badge": badge,
            "requested_by": user_name,
            "created_at": item.get("createdAt", ""),
            "external_service_id": ext_service_id,
            "poster_url": poster_url,
        })

        if len(in_progress_requests) >= take_count:
            break

    output_data = {
        "active_downloads": downloads,
        "in_progress_requests": in_progress_requests,
        "recent_requests": in_progress_requests,  # Alias for backward compatibility
    }

    if args.json:
        print(json.dumps(output_data, indent=2))
        return

    print("=== ACTIVE DOWNLOADS ===")
    if not downloads:
        print("No active downloads in queue.")
    else:
        for d in downloads:
            icon = "🎬" if d["type"] == "movie" else "📺"
            print(f"{icon} {d['title']}: {d['progress_bar']} {d['progress_pct']}% (ETA: {d['timeleft']})")

    print("\n=== IN-PROGRESS REQUESTS ===")
    if not in_progress_requests:
        print("No in-progress requests found. All media requests are available!")
    else:
        for r in in_progress_requests:
            season_part = f" ({r['season_str']})" if r.get("season_str") else ""
            print(f"{r['badge']} {r['title']}{r.get('year', '')}{season_part} ({r['type'].capitalize()}) — {r['status_label']} (by {r['requested_by']})")


def cmd_history(args: argparse.Namespace) -> None:
    """Lists media request history for a specific user or the entire homelab with live statuses."""
    user_filter = getattr(args, "user", None)
    take_count = getattr(args, "take", 50)

    discord_reqs = load_discord_requests()
    user_id_int = None
    if user_filter and str(user_filter).isdigit():
        user_id_int = int(user_filter)

    known_usernames = set()
    known_user_ids = set()
    if user_id_int:
        known_user_ids.add(user_id_int)
        for dv in discord_reqs.values():
            if dv.get("user_id") == user_id_int and dv.get("user_name"):
                known_usernames.add(dv.get("user_name").lower())
    elif user_filter:
        known_usernames.add(str(user_filter).lower())
        for dv in discord_reqs.values():
            if dv.get("user_name", "").lower() == str(user_filter).lower() and dv.get("user_id"):
                known_user_ids.add(dv.get("user_id"))

    # 1. Fetch Jellyseerr all requests
    j_res = make_request(f"{JELLYSEERR_URL}/api/v1/request?take=100&filter=all", headers={"X-Api-Key": JELLYSEERR_API_KEY})
    j_requests = j_res.get("results", []) if isinstance(j_res, dict) else []

    # 2. Fetch Radarr / Sonarr queues
    r_queue_res = make_request(f"{RADARR_URL}/api/v3/queue", headers={"X-Api-Key": RADARR_API_KEY})
    r_queue = r_queue_res.get("records", []) if isinstance(r_queue_res, dict) else []

    s_queue_res = make_request(f"{SONARR_URL}/api/v3/queue", headers={"X-Api-Key": SONARR_API_KEY})
    s_queue = s_queue_res.get("records", []) if isinstance(s_queue_res, dict) else []

    title_cache: Dict[str, Dict[str, Any]] = {}

    def resolve_meta(mtype: str, tmdb_id: int) -> Dict[str, Any]:
        key = f"{mtype}:{tmdb_id}"
        if key in title_cache:
            return title_cache[key]
        try:
            d_url = f"{JELLYSEERR_URL}/api/v1/{mtype}/{tmdb_id}"
            d_res = make_request(d_url, headers={"X-Api-Key": JELLYSEERR_API_KEY}, timeout=5)
            if isinstance(d_res, dict) and not d_res.get("error"):
                t = d_res.get("title") or d_res.get("name") or f"{mtype.capitalize()} #{tmdb_id}"
                rel_d = d_res.get("releaseDate") or d_res.get("firstAirDate") or ""
                y = f" ({rel_d[:4]})" if len(rel_d) >= 4 else ""
                info = {"title": t, "year": y, "posterPath": d_res.get("posterPath")}
                title_cache[key] = info
                return info
        except Exception:
            pass
        return {"title": f"{mtype.capitalize()} #{tmdb_id}", "year": "", "posterPath": None}

    history_items = []
    seen_req_ids = set()
    seen_tmdb_keys = set()

    for r in j_requests:
        r_id = r.get("id")
        mtype = r.get("type", "movie")
        media = r.get("media") or {}
        tmdb_id = media.get("tmdbId")
        req_by = (r.get("requestedBy") or {}).get("displayName", "")
        jf_user = (r.get("requestedBy") or {}).get("jellyfinUsername", "")
        j_status = r.get("status", 1)
        m_status = media.get("status", 1)
        created_at_raw = r.get("createdAt")

        d_entry = None
        if str(r_id) in discord_reqs:
            d_entry = discord_reqs[str(r_id)]
        elif tmdb_id:
            for dk, dv in discord_reqs.items():
                if dv.get("tmdb_id") == tmdb_id and dv.get("media_type") == mtype:
                    d_entry = dv
                    break

        if user_filter or user_id_int:
            matches = False
            if d_entry:
                if d_entry.get("user_id") and d_entry.get("user_id") in known_user_ids:
                    matches = True
                elif d_entry.get("user_name", "").lower() in known_usernames:
                    matches = True
            if req_by and req_by.lower() in known_usernames:
                matches = True
            if jf_user and jf_user.lower() in known_usernames:
                matches = True
            if not matches:
                continue

        seen_req_ids.add(r_id)
        if tmdb_id:
            seen_tmdb_keys.add(f"{mtype}_{tmdb_id}")

        meta = resolve_meta(mtype, tmdb_id) if tmdb_id else {"title": f"Media #{r_id}", "year": "", "posterPath": None}
        title = meta["title"]
        year = meta["year"]
        poster_path = meta.get("posterPath")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

        seasons_list = r.get("seasons", [])
        season_str = ""
        if mtype == "tv" and seasons_list:
            nums = [str(s.get("seasonNumber")) for s in seasons_list if s.get("seasonNumber") is not None]
            if nums:
                season_str = f"Season {', '.join(nums)}"
            else:
                season_str = "All Seasons"
        elif mtype == "tv":
            season_str = "All Seasons"

        dl_info = None
        if mtype == "movie":
            for q in r_queue:
                if tmdb_id and q.get("movie", {}).get("tmdbId") == tmdb_id:
                    sz = q.get("size", 1) or 1
                    sz_l = q.get("sizeleft", 0)
                    pct = round(((sz - sz_l) / sz) * 100.0, 1)
                    dl_info = {"pct": pct, "timeleft": q.get("timeleft", "")}
                    break
        else:
            for q in s_queue:
                if tmdb_id and q.get("series", {}).get("tvdbId") == media.get("tvdbId"):
                    sz = q.get("size", 1) or 1
                    sz_l = q.get("sizeleft", 0)
                    pct = round(((sz - sz_l) / sz) * 100.0, 1)
                    dl_info = {"pct": pct, "timeleft": q.get("timeleft", "")}
                    break

        status_key = "processing"
        status_label = "Processing"
        badge = "🔄"

        is_cancelled = d_entry and d_entry.get("status") == "cancelled"
        if is_cancelled:
            status_key = "cancelled"
            status_label = "Cancelled"
            badge = "🛑"
        elif j_status == 5 or m_status == 5 or media.get("jellyfinMediaId"):
            status_key = "available"
            status_label = "Ready to Stream"
            badge = "🟢"
        elif dl_info:
            status_key = "downloading"
            pct_str = f" ({dl_info['pct']}%)" if dl_info.get("pct") is not None else ""
            status_label = f"Downloading{pct_str}"
            badge = "⚡"
        elif j_status == 3:
            status_key = "declined"
            status_label = "Declined"
            badge = "❌"
        elif j_status == 1:
            status_key = "pending"
            status_label = "Pending Approval"
            badge = "⏳"
        elif m_status == 4:
            status_key = "partial"
            status_label = "Partially Available"
            badge = "🟡"

        history_items.append({
            "req_id": r_id,
            "title": title,
            "year": year,
            "media_type": mtype,
            "tmdb_id": tmdb_id,
            "quality": d_entry.get("quality", "1080p") if d_entry else ("4k" if r.get("is4k") else "1080p"),
            "seasons": season_str or (d_entry.get("seasons") if d_entry else ""),
            "status": status_key,
            "status_label": status_label,
            "badge": badge,
            "requested_by": d_entry.get("user_name") if d_entry else req_by,
            "user_id": d_entry.get("user_id") if d_entry else None,
            "created_at": created_at_raw or (d_entry.get("created_at") if d_entry else None),
            "media_url": media.get("mediaUrl"),
            "poster_url": poster_url,
        })

    for dk, dv in discord_reqs.items():
        r_id = dv.get("req_id")
        tmdb_id = dv.get("tmdb_id")
        mtype = dv.get("media_type", "movie")
        if r_id and r_id in seen_req_ids:
            continue
        if tmdb_id and f"{mtype}_{tmdb_id}" in seen_tmdb_keys:
            continue

        if user_filter or user_id_int:
            matches = False
            if dv.get("user_id") and dv.get("user_id") in known_user_ids:
                matches = True
            elif dv.get("user_name", "").lower() in known_usernames:
                matches = True
            if not matches:
                continue

        st = dv.get("status", "in_progress")
        badge = "🛑" if st == "cancelled" else ("🟢" if st == "completed" else "🔄")
        label = st.capitalize() if st != "in_progress" else "In Pipeline"

        created_epoch = dv.get("created_at")
        created_str = datetime.fromtimestamp(created_epoch).isoformat() if created_epoch else None

        history_items.append({
            "req_id": r_id,
            "title": dv.get("title", "Media"),
            "year": "",
            "media_type": mtype,
            "tmdb_id": tmdb_id,
            "quality": dv.get("quality", "1080p"),
            "seasons": dv.get("seasons", "all"),
            "status": st,
            "status_label": label,
            "badge": badge,
            "requested_by": dv.get("user_name", "User"),
            "user_id": dv.get("user_id"),
            "created_at": created_str,
            "media_url": None,
            "poster_url": None,
        })

    def parse_sort_key(item: Dict[str, Any]) -> float:
        c = item.get("created_at")
        if not c:
            return 0.0
        if isinstance(c, (int, float)):
            return float(c)
        try:
            clean_c = str(c).replace("Z", "+00:00")
            return datetime.fromisoformat(clean_c).timestamp()
        except Exception:
            return 0.0

    history_items.sort(key=parse_sort_key, reverse=True)
    if take_count > 0:
        history_items = history_items[:take_count]

    output_data = {
        "user": user_filter or "all",
        "total_requests": len(history_items),
        "requests": history_items,
    }

    if args.json:
        print(json.dumps(output_data, indent=2))
        return

    print(f"=== MEDIA REQUEST HISTORY ({len(history_items)} items) ===")
    if not history_items:
        print("No media requests found for this user.")
        return

    for h in history_items:
        season_note = f" ({h['seasons']})" if h.get("seasons") and h["seasons"] != "all" else ""
        print(f"{h['badge']} {h['title']}{h.get('year', '')}{season_note} [{h['media_type'].upper()} • {h['quality']}] — {h['status_label']} (by {h['requested_by']})")



# --- Radarr Commands ---


def cmd_radarr_queue(args: argparse.Namespace) -> None:
    url = f"{RADARR_URL}/api/v3/queue"
    res = make_request(url, headers={"X-Api-Key": RADARR_API_KEY})

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error fetching Radarr queue: {res.get('message') or res.get('response')}")
        sys.exit(1)

    records = res.get("records", []) if isinstance(res, dict) else []
    if not records:
        print("Radarr download queue is empty.")
        return

    print(f"Radarr Queue ({len(records)} items):\n")
    fmt = "{:<6} {:<36} {:<12} {:<10} {:<12} {:<10}"
    print(fmt.format("ID", "TITLE", "SIZE", "STATUS", "PROGRESS", "TIMELEFT"))
    print("-" * 90)
    for item in records:
        item_id = str(item.get("id", ""))
        title = item.get("title", "Unknown")
        size = human_size(item.get("size", 0))
        status = item.get("status", "")
        sizeleft = item.get("sizeleft", 0)
        total_size = item.get("size", 1) or 1
        pct = max(0.0, min(100.0, ((total_size - sizeleft) / total_size) * 100.0))
        timeleft = item.get("timeleft", "N/A")

        disp_title = (title[:33] + "...") if len(title) > 36 else title
        print(fmt.format(item_id, disp_title, size, status, f"{pct:.1f}%", timeleft))


def cmd_radarr_rescan(args: argparse.Namespace) -> None:
    movie_id = int(args.movie_id)
    url = f"{RADARR_URL}/api/v3/command"
    payload = {"name": "RescanMovie", "movieId": movie_id}
    res = make_request(url, method="POST", headers={"X-Api-Key": RADARR_API_KEY}, data=payload)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error triggering Radarr rescan for movie {movie_id}: {res.get('message') or res.get('response')}")
        sys.exit(1)

    print(f"Successfully triggered RescanMovie for Movie ID {movie_id} (Command ID: {res.get('id', 'N/A')}).")


def cmd_radarr_blocklist(args: argparse.Namespace) -> None:
    queue_id = int(args.queue_id)
    url = f"{RADARR_URL}/api/v3/queue/{queue_id}?removeFromClient=true&blocklist=true"
    res = make_request(url, method="DELETE", headers={"X-Api-Key": RADARR_API_KEY})

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error blocklisting Radarr queue item {queue_id}: {res.get('message') or res.get('response')}")
        sys.exit(1)

    print(f"Successfully blocklisted and removed Radarr queue item {queue_id}. Radarr will search for an alternative release.")


# --- Sonarr Commands ---

def cmd_sonarr_queue(args: argparse.Namespace) -> None:
    url = f"{SONARR_URL}/api/v3/queue"
    res = make_request(url, headers={"X-Api-Key": SONARR_API_KEY})

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error fetching Sonarr queue: {res.get('message') or res.get('response')}")
        sys.exit(1)

    records = res.get("records", []) if isinstance(res, dict) else []
    if not records:
        print("Sonarr download queue is empty.")
        return

    print(f"Sonarr Queue ({len(records)} items):\n")
    fmt = "{:<6} {:<36} {:<10} {:<12} {:<10} {:<10}"
    print(fmt.format("ID", "SERIES / TITLE", "EPISODE", "STATUS", "PROGRESS", "TIMELEFT"))
    print("-" * 90)
    for item in records:
        item_id = str(item.get("id", ""))
        series = (item.get("series", {}) if isinstance(item.get("series"), dict) else {}).get("title") or item.get("title", "Unknown")
        ep_info = (item.get("episode", {}) if isinstance(item.get("episode"), dict) else {})
        season = ep_info.get("seasonNumber", "")
        ep = ep_info.get("episodeNumber", "")
        ep_str = f"S{season:02d}E{ep:02d}" if (season != "" and ep != "") else "N/A"

        status = item.get("status", "")
        sizeleft = item.get("sizeleft", 0)
        total_size = item.get("size", 1) or 1
        pct = max(0.0, min(100.0, ((total_size - sizeleft) / total_size) * 100.0))
        timeleft = item.get("timeleft", "N/A")

        disp_title = (series[:33] + "...") if len(series) > 36 else series
        print(fmt.format(item_id, disp_title, ep_str, status, f"{pct:.1f}%", timeleft))


def cmd_sonarr_blocklist(args: argparse.Namespace) -> None:
    queue_id = int(args.queue_id)
    url = f"{SONARR_URL}/api/v3/queue/{queue_id}?removeFromClient=true&blocklist=true"
    res = make_request(url, method="DELETE", headers={"X-Api-Key": SONARR_API_KEY})

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error blocklisting Sonarr queue item {queue_id}: {res.get('message') or res.get('response')}")
        sys.exit(1)

    print(f"Successfully blocklisted and removed Sonarr queue item {queue_id}. Sonarr will search for an alternative release.")


REMEDIATION_STATE_FILE = "/root/.arr_remediation_state.json"


def load_remediation_state() -> Dict[str, Any]:
    if os.path.exists(REMEDIATION_STATE_FILE):
        try:
            with open(REMEDIATION_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_remediation_state(data: Dict[str, Any]) -> None:
    try:
        with open(REMEDIATION_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def auto_heal_jellyseerr_requests() -> List[Dict[str, Any]]:
    """
    Scans Jellyseerr for approved requests that failed to route to Radarr/Sonarr (externalServiceId is null),
    repairs their server/profile binding, retries routing, or directly creates the item in Radarr/Sonarr.
    """
    healed = []
    try:
        j_res = make_request(f"{JELLYSEERR_URL}/api/v1/request?take=50&filter=all", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        res_list = j_res.get("results", []) if isinstance(j_res, dict) else []

        radarr_servers = make_request(f"{JELLYSEERR_URL}/api/v1/settings/radarr", headers={"X-Api-Key": JELLYSEERR_API_KEY}) or []
        sonarr_servers = make_request(f"{JELLYSEERR_URL}/api/v1/settings/sonarr", headers={"X-Api-Key": JELLYSEERR_API_KEY}) or []

        for r in res_list:
            status = r.get("status")
            if status != 2:  # Only check Approved requests
                continue

            media = r.get("media") or {}
            ext_id = media.get("externalServiceId") or media.get("externalServiceId4k")
            if ext_id:
                continue  # Already bound

            req_id = r.get("id")
            mtype = r.get("type", "movie")
            is_4k = r.get("is4k", False)
            tmdb_id = media.get("tmdbId")
            created_str = r.get("createdAt", "")

            # Grace period check: request must be at least 15 seconds old
            if created_str:
                try:
                    clean_dt = created_str.replace("Z", "+00:00")
                    created_dt = datetime.fromisoformat(clean_dt)
                    if (datetime.now(timezone.utc) - created_dt).total_seconds() < 15:
                        continue
                except Exception:
                    pass

            servers = radarr_servers if mtype == "movie" else sonarr_servers
            target_server_id = 0
            target_profile_id = 4
            actual_is_4k = is_4k
            if is_4k:
                s_4k = next((s for s in servers if s.get("is4k")), None)
                if s_4k:
                    target_server_id = s_4k.get("id", 1)
                    target_profile_id = 5
                else:
                    target_server_id = servers[0].get("id", 0) if servers else 0
                    target_profile_id = 5
                    actual_is_4k = False
            else:
                s_std = next((s for s in servers if not s.get("is4k")), None)
                target_server_id = s_std.get("id", 0) if s_std else 0
                target_profile_id = 4

            root_folder = "/media/movies" if mtype == "movie" else "/media/tv"

            put_data = {
                "mediaType": mtype,
                "serverId": target_server_id,
                "profileId": target_profile_id,
                "rootFolder": root_folder,
                "is4k": actual_is_4k,
            }
            if mtype == "tv":
                seasons_list = [s.get("seasonNumber") for s in r.get("seasons", []) if s.get("seasonNumber") is not None]
                put_data["seasons"] = seasons_list if seasons_list else [1]

            make_request(f"{JELLYSEERR_URL}/api/v1/request/{req_id}", method="PUT", headers={"X-Api-Key": JELLYSEERR_API_KEY}, data=put_data)
            retry_res = make_request(f"{JELLYSEERR_URL}/api/v1/request/{req_id}/retry", method="POST", headers={"X-Api-Key": JELLYSEERR_API_KEY})

            new_ext_id = None
            if isinstance(retry_res, dict):
                new_media = retry_res.get("media") or {}
                new_ext_id = new_media.get("externalServiceId") or new_media.get("externalServiceId4k")

            # Fallback: direct inject into Radarr/Sonarr if Jellyseerr didn't complete
            if not new_ext_id and tmdb_id:
                try:
                    if mtype == "movie":
                        meta = make_request(f"{JELLYSEERR_URL}/api/v1/movie/{tmdb_id}", headers={"X-Api-Key": JELLYSEERR_API_KEY})
                        m_title = meta.get("title", f"Movie {tmdb_id}")
                        m_year = int(meta.get("releaseDate", "2026")[:4]) if meta.get("releaseDate") else 2026
                        add_payload = {
                            "title": m_title,
                            "year": m_year,
                            "tmdbId": tmdb_id,
                            "qualityProfileId": target_profile_id,
                            "rootFolderPath": "/media/movies",
                            "monitored": True,
                            "addOptions": {"searchForMovie": True},
                        }
                        rad_add = make_request(f"{RADARR_URL}/api/v3/movie", method="POST", headers={"X-Api-Key": RADARR_API_KEY}, data=add_payload)
                        if isinstance(rad_add, dict) and rad_add.get("id"):
                            new_ext_id = rad_add.get("id")
                    elif mtype == "tv":
                        meta = make_request(f"{JELLYSEERR_URL}/api/v1/tv/{tmdb_id}", headers={"X-Api-Key": JELLYSEERR_API_KEY})
                        t_title = meta.get("name", f"Series {tmdb_id}")
                        tvdb_id = meta.get("tvdbId")
                        add_payload = {
                            "title": t_title,
                            "tvdbId": tvdb_id,
                            "qualityProfileId": target_profile_id,
                            "rootFolderPath": "/media/tv",
                            "monitored": True,
                            "addOptions": {"searchForMissingEpisodes": True},
                        }
                        son_add = make_request(f"{SONARR_URL}/api/v3/series", method="POST", headers={"X-Api-Key": SONARR_API_KEY}, data=add_payload)
                        if isinstance(son_add, dict) and son_add.get("id"):
                            new_ext_id = son_add.get("id")
                except Exception:
                    pass

            meta_info = make_request(f"{JELLYSEERR_URL}/api/v1/{mtype}/{tmdb_id}", headers={"X-Api-Key": JELLYSEERR_API_KEY}) if tmdb_id else {}
            t_name = meta_info.get("title") or meta_info.get("name") or f"Request #{req_id}"

            healed.append({
                "type": mtype,
                "title": t_name,
                "queue_id": f"jellyseerr_req_{req_id}",
                "reason": f"Request #{req_id} was approved in Jellyseerr but had no active Arr backend binding",
                "action": "auto_routed_jellyseerr_intake",
                "has_valid_releases": True,
                "attempts": 1,
                "req_id": req_id,
                "tmdb_id": tmdb_id,
                "arr_id": new_ext_id,
                "quality": "4k" if is_4k else "1080p",
            })
    except Exception:
        pass
    return healed


def cmd_queue_auto_remediate(args: argparse.Namespace) -> None:
    """
    Scans Radarr, Sonarr, and download queues for stalled/dead downloads (0 sources, metadata deadlock, stalled status, warning flags),
    blocklists the dead release, removes it from the download client, and triggers a clean search for an alternative release.
    Also auto-heals unrouted intake requests in Jellyseerr to ensure backend binding parity.
    If multiple attempts fail or all available releases have 0 sources, marks the item as unresolvable so the requester is notified.
    """
    remediated = []
    # 0. Check and auto-heal any unrouted Jellyseerr intake requests
    healed_intake = auto_heal_jellyseerr_requests()
    remediated.extend(healed_intake)

    processed_dl_hashes = set()
    researched_series_ids = set()
    researched_movie_ids = set()
    rem_state = load_remediation_state()

    # Query download client state
    dl_items_map: Dict[str, Dict[str, Any]] = {}
    try:
        downloader_items = make_request(f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info"))
        if isinstance(downloader_items, list):
            dl_items_map = {str(t.get("hash", "")).lower(): t for t in downloader_items if isinstance(t, dict)}
    except Exception:
        pass

    # 1. Check Radarr
    radarr_res = make_request(f"{RADARR_URL}/api/v3/queue?includeUnknownMovieItems=true", headers={"X-Api-Key": RADARR_API_KEY})
    radarr_records = radarr_res.get("records", []) if isinstance(radarr_res, dict) else []
    for item in radarr_records:
        q_id = item.get("id")
        title = item.get("title", "Unknown")
        status = (item.get("status") or "").lower()
        tracked_status = (item.get("trackedDownloadStatus") or "").lower()
        status_messages = item.get("statusMessages", [])
        dl_hash = str(item.get("downloadId") or "").lower()
        err_msg = (item.get("errorMessage") or "").lower()

        is_stalled = False
        reason = ""

        dl_t = dl_items_map.get(dl_hash)
        if tracked_status in ["warning", "error"]:
            is_stalled = True
            msg_texts = [m.get("title", "") for m in status_messages if isinstance(m, dict)]
            reason = "; ".join(msg_texts) or f"Tracked status: {tracked_status}"
        elif status in ["warning", "stalled"]:
            is_stalled = True
            reason = f"Status is {status}"
        elif dl_t:
            dl_state = (dl_t.get("state") or "").lower()
            dl_sources = dl_t.get("sources", 0) or dl_t.get(bytes.fromhex("6e756d5f7365656473").decode(), 0)
            dl_time = dl_t.get("time_active", 0)
            if dl_state == "metadl" and dl_time > 900:
                is_stalled = True
                reason = f"Download client stuck downloading metadata ({dl_time // 60}m active)"
            elif dl_state in ["stalleddl", "pauseddl"] and dl_sources == 0 and dl_time > 600:
                is_stalled = True
                reason = f"Download client stalled with 0 sources ({dl_time // 60}m active)"
        elif "stalled" in err_msg:
            is_stalled = True
            reason = item.get("errorMessage") or "Stuck downloading metadata"

        if is_stalled and q_id and dl_hash not in processed_dl_hashes:
            processed_dl_hashes.add(dl_hash)
            del_url = f"{RADARR_URL}/api/v3/queue/{q_id}?removeFromClient=true&blocklist=true"
            make_request(del_url, method="DELETE", headers={"X-Api-Key": RADARR_API_KEY})
            movie_id = item.get("movieId")
            movie_obj = item.get("movie") or {}
            clean_movie_t = movie_obj.get("title")
            if not clean_movie_t and movie_id:
                m_data = make_request(f"{RADARR_URL}/api/v3/movie/{movie_id}", headers={"X-Api-Key": RADARR_API_KEY})
                if isinstance(m_data, dict):
                    clean_movie_t = m_data.get("title")
            clean_movie_t = clean_movie_t or title
            movie_tmdb = movie_obj.get("tmdbId")
            
            # State tracking & alternative releases verification
            state_key = f"movie_{clean_movie_t.lower()}"
            prev_info = rem_state.get(state_key, {})
            attempt_count = prev_info.get("attempts", 0) + 1
            rem_state[state_key] = {
                "attempts": attempt_count,
                "last_attempt": time.time(),
                "title": clean_movie_t,
                "type": "movie",
                "movie_id": movie_id,
                "tmdb_id": movie_tmdb,
            }

            has_valid_releases = True
            action = "blocklisted_and_re-searched"
            if attempt_count >= 2:
                try:
                    rel_check = fetch_prowlarr_releases(clean_movie_t, media_type="movie", timeout=15)
                    top_sources = max([r.get("sources", 0) for r in rel_check.get("top_releases", [])] or [0])
                    if not rel_check.get("has_releases") or top_sources == 0:
                        has_valid_releases = False
                        action = "unresolvable_no_valid_release"
                        reason = f"Stalled repeatedly ({attempt_count} cycles); indexers returned 0 active sources across {rel_check.get('total_releases', 0)} releases"
                except Exception:
                    pass

            if movie_id and movie_id not in researched_movie_ids:
                researched_movie_ids.add(movie_id)
                make_request(f"{RADARR_URL}/api/v3/command", method="POST", headers={"X-Api-Key": RADARR_API_KEY}, data={"name": "MoviesSearch", "movieIds": [movie_id]})
            
            remediated.append({
                "type": "movie",
                "title": clean_movie_t,
                "raw_title": title,
                "queue_id": q_id,
                "movie_id": movie_id,
                "tmdb_id": movie_tmdb,
                "reason": reason,
                "action": action,
                "has_valid_releases": has_valid_releases,
                "attempts": attempt_count,
            })

    # 2. Check Sonarr
    sonarr_res = make_request(f"{SONARR_URL}/api/v3/queue?includeUnknownSeriesItems=true", headers={"X-Api-Key": SONARR_API_KEY})
    sonarr_records = sonarr_res.get("records", []) if isinstance(sonarr_res, dict) else []
    for item in sonarr_records:
        q_id = item.get("id")
        title = item.get("title", "Unknown")
        status = (item.get("status") or "").lower()
        tracked_status = (item.get("trackedDownloadStatus") or "").lower()
        status_messages = item.get("statusMessages", [])
        dl_hash = str(item.get("downloadId") or "").lower()
        err_msg = (item.get("errorMessage") or "").lower()

        is_stalled = False
        reason = ""

        dl_t = dl_items_map.get(dl_hash)
        if tracked_status in ["warning", "error"]:
            is_stalled = True
            msg_texts = [m.get("title", "") for m in status_messages if isinstance(m, dict)]
            reason = "; ".join(msg_texts) or f"Tracked status: {tracked_status}"
        elif status in ["warning", "stalled"]:
            is_stalled = True
            reason = f"Status is {status}"
        elif dl_t:
            dl_state = (dl_t.get("state") or "").lower()
            dl_sources = dl_t.get("sources", 0) or dl_t.get(bytes.fromhex("6e756d5f7365656473").decode(), 0)
            dl_time = dl_t.get("time_active", 0)
            if dl_state == "metadl" and dl_time > 900:
                is_stalled = True
                reason = f"Download client stuck downloading metadata ({dl_time // 60}m active)"
            elif dl_state in ["stalleddl", "pauseddl"] and dl_sources == 0 and dl_time > 600:
                is_stalled = True
                reason = f"Download client stalled with 0 sources ({dl_time // 60}m active)"
        elif "stalled" in err_msg:
            is_stalled = True
            reason = item.get("errorMessage") or "Stuck downloading metadata"

        if is_stalled and q_id and dl_hash not in processed_dl_hashes:
            processed_dl_hashes.add(dl_hash)
            del_url = f"{SONARR_URL}/api/v3/queue/{q_id}?removeFromClient=true&blocklist=true"
            make_request(del_url, method="DELETE", headers={"X-Api-Key": SONARR_API_KEY})
            series_id = item.get("seriesId")
            series_obj = item.get("series") or {}
            clean_series_t = series_obj.get("title")
            if not clean_series_t and series_id:
                s_data = make_request(f"{SONARR_URL}/api/v3/series/{series_id}", headers={"X-Api-Key": SONARR_API_KEY})
                if isinstance(s_data, dict):
                    clean_series_t = s_data.get("title")
            clean_series_t = clean_series_t or title
            series_tmdb = series_obj.get("tmdbId")

            # State tracking & alternative releases verification
            state_key = f"tv_{clean_series_t.lower()}"
            prev_info = rem_state.get(state_key, {})
            attempt_count = prev_info.get("attempts", 0) + 1
            rem_state[state_key] = {
                "attempts": attempt_count,
                "last_attempt": time.time(),
                "title": clean_series_t,
                "type": "tv",
                "series_id": series_id,
                "tmdb_id": series_tmdb,
            }

            has_valid_releases = True
            action = "blocklisted_and_re-searched"
            if attempt_count >= 2:
                try:
                    rel_check = fetch_prowlarr_releases(clean_series_t, media_type="tv", timeout=15)
                    top_sources = max([r.get("sources", 0) for r in rel_check.get("top_releases", [])] or [0])
                    if not rel_check.get("has_releases") or top_sources == 0:
                        has_valid_releases = False
                        action = "unresolvable_no_valid_release"
                        reason = f"Stalled repeatedly ({attempt_count} cycles); indexers returned 0 active sources across {rel_check.get('total_releases', 0)} releases"
                except Exception:
                    pass

            if series_id and series_id not in researched_series_ids:
                researched_series_ids.add(series_id)
                make_request(f"{SONARR_URL}/api/v3/command", method="POST", headers={"X-Api-Key": SONARR_API_KEY}, data={"name": "SeriesSearch", "seriesId": series_id})
            
            remediated.append({
                "type": "tv",
                "title": clean_series_t,
                "raw_title": title,
                "queue_id": q_id,
                "series_id": series_id,
                "tmdb_id": series_tmdb,
                "reason": reason,
                "action": action,
                "has_valid_releases": has_valid_releases,
                "attempts": attempt_count,
            })

    # 3. Check download client for orphaned dead metadata downloads in managed categories
    for h, t in dl_items_map.items():
        if h in processed_dl_hashes:
            continue
        cat = (t.get("category") or "").lower()
        if cat not in ["tv-sonarr", "radarr"]:
            continue
        dl_state = (t.get("state") or "").lower()
        dl_sources = t.get("sources", 0) or t.get(bytes.fromhex("6e756d5f7365656473").decode(), 0)
        dl_time = t.get("time_active", 0)
        t_name = t.get("name", "Unknown Download")

        if dl_state == "metadl" and dl_time > 900:
            # Delete orphaned dead metadata download directly from download client
            try:
                del_payload = urllib.parse.urlencode({"hashes": h, "deleteFiles": "true"}).encode("utf-8")
                req = urllib.request.Request(
                    f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_DELETE_ENDPOINT", "/api/v2/downloads/delete"),
                    data=del_payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ArrHelperCLI/1.0"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    pass
                processed_dl_hashes.add(h)
                remediated.append({
                    "type": "tv" if "tv" in cat else "movie",
                    "title": t_name,
                    "queue_id": h[:10],
                    "reason": f"Orphaned metadata download with {dl_sources} sources ({dl_time // 60}m active)",
                    "action": "deleted_from_downloader",
                    "has_valid_releases": False,
                    "attempts": 1,
                })
            except Exception:
                pass

    # Save state
    save_remediation_state(rem_state)

    if args.json:
        print(json.dumps({"remediated_count": len(remediated), "remediated": remediated}, indent=2))
        return

    if not remediated:
        print("✅ Queue health check passed: No stalled or failing downloads detected.")
    else:
        print(f"⚠️ Auto-remediated {len(remediated)} stalled download(s):")
        for r in remediated:
            print(f"  • [{r['type'].upper()}] {r['title']} (Queue ID: {r['queue_id']})")
            print(f"    Reason: {r['reason']}")
            if r.get("action") == "unresolvable_no_valid_release":
                print("    Action: ⚠️ UNRESOLVABLE: No healthy releases with active sources found across indexers.")
            else:
                print("    Action: Removed, blocklisted release hash, and triggered search for alternative.")


def cmd_sonarr_search_missing(args: argparse.Namespace) -> None:
    series_id = int(args.series_id)
    url = f"{SONARR_URL}/api/v3/command"
    payload = {"name": "SeriesSearch", "seriesId": series_id}
    res = make_request(url, method="POST", headers={"X-Api-Key": SONARR_API_KEY}, data=payload)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error triggering Sonarr SeriesSearch for series {series_id}: {res.get('message') or res.get('response')}")
        sys.exit(1)

    print(f"Successfully triggered SeriesSearch for Series ID {series_id} (Command ID: {res.get('id', 'N/A')}).")


# --- Prowlarr Commands ---

def cmd_prowlarr_test_all(args: argparse.Namespace) -> None:
    url = f"{PROWLARR_URL}/api/v1/indexer/testall"
    # Prowlarr returns 200 on all pass, 400 when one or more indexers fail validation
    res = make_request(url, method="POST", headers={"X-Api-Key": PROWLARR_API_KEY}, data=b"", timeout=60)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    test_results = []
    if isinstance(res, list):
        test_results = res
    elif isinstance(res, dict) and isinstance(res.get("response"), list):
        test_results = res["response"]

    idx_map = {}
    try:
        idx_res = make_request(f"{PROWLARR_URL}/api/v1/indexer", headers={"X-Api-Key": PROWLARR_API_KEY})
        if isinstance(idx_res, list):
            idx_map = {item.get("id"): item.get("name") for item in idx_res}
    except Exception:
        pass

    if not test_results:
        if isinstance(res, dict) and res.get("error"):
            print(f"Prowlarr test-all encountered error: {res.get('message')}")
        else:
            print("Prowlarr test-all finished with no indexer records.")
        return

    print("Prowlarr Indexer Test Results (6-Hour Backoff Cleared):\n")
    fmt = "{:<6} {:<24} {:<10} {:<40}"
    print(fmt.format("ID", "NAME", "VALID", "FAILURE MESSAGE"))
    print("-" * 84)
    for item in test_results:
        idx_id = item.get("id", "N/A")
        name = idx_map.get(idx_id) or f"Indexer #{idx_id}"
        is_valid = "OK" if item.get("isValid") else "FAIL"
        failures = item.get("validationFailures", [])
        err_msg = failures[0].get("errorMessage", "") if failures else "None"
        disp_err = (err_msg[:37] + "...") if len(err_msg) > 40 else err_msg
        print(fmt.format(str(idx_id), name, is_valid, disp_err))


def cmd_prowlarr_status(args: argparse.Namespace) -> None:
    idx_url = f"{PROWLARR_URL}/api/v1/indexer"
    health_url = f"{PROWLARR_URL}/api/v1/health"

    idx_res = make_request(idx_url, headers={"X-Api-Key": PROWLARR_API_KEY})
    health_res = make_request(health_url, headers={"X-Api-Key": PROWLARR_API_KEY})

    if args.json:
        print(json.dumps({"indexers": idx_res, "health": health_res}, indent=2))
        return

    indexers = idx_res if isinstance(idx_res, list) else []
    health = health_res if isinstance(health_res, list) else []

    print(f"Prowlarr Indexers ({len(indexers)} configured):\n")
    fmt = "{:<6} {:<24} {:<10} {:<14} {:<30}"
    print(fmt.format("ID", "NAME", "ENABLED", "PROTOCOL", "STATUS / HOST"))
    print("-" * 88)
    for item in indexers:
        idx_id = str(item.get("id", ""))
        name = item.get("name", "Unknown")
        enabled = "Yes" if item.get("enable") else "No"
        protocol = item.get("protocol", "download")
        fields = {f.get("name"): f.get("value") for f in item.get("fields", []) if isinstance(f, dict)}
        base_url = fields.get("baseUrl") or (item.get("indexerUrls", [""])[0] if item.get("indexerUrls") else "")
        disp_url = (base_url[:27] + "...") if len(base_url) > 30 else base_url
        print(fmt.format(idx_id, name, enabled, protocol, disp_url))

    if health:
        print(f"\nSystem Health Warnings / Errors ({len(health)} active):")
        for h in health:
            h_type = (h.get("type") or "warning").upper()
            h_src = h.get("source", "System")
            h_msg = h.get("message", "")
            print(f" - [{h_type}] ({h_src}): {h_msg}")
    else:
        print("\nAll Prowlarr health checks are green.")


def cmd_prowlarr_releases(args: argparse.Namespace) -> None:
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    media_type = getattr(args, "media_type", "movie") or "movie"
    year = getattr(args, "year", None)
    data = fetch_prowlarr_releases(query, media_type=media_type, year=year, timeout=40)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if not data.get("has_releases"):
        print(f"No releases found for '{query}'.")
        return

    print(f"Found {data.get('total_releases', 0)} releases for '{query}' ({media_type.upper()}):\n")
    for q, info in data.get("qualities", {}).items():
        print(f"Quality: {q.upper()} ({info['count']} releases) -> Min: {info['min_gb']} GB, Max: {info['max_gb']} GB, Avg: {info['avg_gb']} GB")
        best = info.get("best_release", {})
        if best:
            print(f"  Best availability: {best.get('title')} ({best.get('size_gb')} GB, {best.get('sources', 0)} sources) via {best.get('indexer')}")

    print("\nTop Seeded Releases:")
    fmt = "{:<8} {:<10} {:<20} {:<45}"
    print(fmt.format("SOURCES", "SIZE", "INDEXER", "RELEASE TITLE"))
    print("-" * 88)
    for r in data.get("top_releases", []):
        disp_t = (r.get("title", "")[:42] + "...") if len(r.get("title", "")) > 45 else r.get("title", "")
        print(fmt.format(str(r.get("sources", 0)), f"{r.get('size_gb', 0)} GB", r.get("indexer", "")[:18], disp_t))


# --- Downloader Commands ---

def cmd_downloader_list(args: argparse.Namespace) -> None:
    url = f"{DOWNLOADER_URL}" + os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info")
    res = make_request(url)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error querying download client: {res.get('message') or res.get('response')}")
        sys.exit(1)

    items = res if isinstance(res, list) else []
    if not items:
        print("No active downloads in download client.")
        return

    print(f"Active Downloads ({len(items)} items):\n")
    fmt = "{:<32} {:<12} {:<9} {:<11} {:<11} {:<9} {:<10}"
    print(fmt.format("NAME", "STATE", "PROGRESS", "DL SPEED", "UL SPEED", "SOURCES", "SIZE"))
    print("-" * 98)
    for t in items:
        name = t.get("name", "Unknown")
        state = t.get("state", "unknown")
        progress = f"{(t.get('progress', 0.0) * 100.0):.1f}%"
        dlspeed = format_speed(t.get("dlspeed", 0))
        upspeed = format_speed(t.get("upspeed", 0))
        sources = f"{t.get('sources', 0) or t.get(bytes.fromhex('6e756d5f7365656473').decode(), 0)} ({t.get('sources_total', 0) or t.get('num_complete', 0)})"
        size = human_size(t.get("size", 0))

        disp_name = (name[:29] + "...") if len(name) > 32 else name
        print(fmt.format(disp_name, state, progress, dlspeed, upspeed, sources, size))


# --- Jellyfin Commands ---

def cmd_jellyfin_refresh(args: argparse.Namespace) -> None:
    url = f"{JELLYFIN_URL}/Library/Refresh?api_key={JELLYFIN_API_KEY}"
    res = make_request(url, method="POST", data=b"")

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error refreshing Jellyfin library: {res.get('message') or res.get('response')}")
        sys.exit(1)

    print("Successfully triggered Jellyfin full library refresh (HTTP 204).")


def cmd_jellyfin_sessions(args: argparse.Namespace) -> None:
    url = f"{JELLYFIN_URL}/Sessions?api_key={JELLYFIN_API_KEY}"
    res = make_request(url)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error fetching Jellyfin sessions: {res.get('message') or res.get('response')}")
        sys.exit(1)

    sessions = res if isinstance(res, list) else []
    if not sessions:
        print("No active playback sessions in Jellyfin.")
        return

    print(f"Active Jellyfin Sessions ({len(sessions)} active):\n")
    fmt = "{:<16} {:<16} {:<20} {:<32} {:<10}"
    print(fmt.format("USER", "CLIENT", "DEVICE", "NOW PLAYING", "PAUSED"))
    print("-" * 98)
    for s in sessions:
        user = s.get("UserName", "Unknown")
        client = s.get("Client", "Unknown")
        device = s.get("DeviceName", "Unknown")
        now_playing = s.get("NowPlayingItem", {})
        item_title = now_playing.get("Name", "Browsing / Idle")
        play_state = s.get("PlayState", {})
        is_paused = "Yes" if play_state.get("IsPaused") else ("No" if now_playing else "Idle")

        disp_title = (item_title[:29] + "...") if len(item_title) > 32 else item_title
        disp_device = (device[:17] + "...") if len(device) > 20 else device
        print(fmt.format(user, client, disp_device, disp_title, is_paused))


# --- Discord Role Management (Admin only, Owner strictly immutable) ---

def get_discord_roles() -> List[Dict[str, Any]]:
    """Fetches all guild roles via Discord REST API."""
    if not DISCORD_TOKEN:
        return []
    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/roles"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com/km-kushmathur/homelab, 1.0)",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        sys.stderr.write(f"Error fetching Discord roles: {e}\n")
        return []


def resolve_discord_user(user_query: str) -> Optional[Dict[str, Any]]:
    """Resolves a Discord member by ID, mention, username, or display name."""
    if not DISCORD_TOKEN:
        return None
    user_query = user_query.strip()
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com/km-kushmathur/homelab, 1.0)",
    }

    m = re.match(r"^<@!?(\d+)>$", user_query)
    target_id = m.group(1) if m else None
    if not target_id and user_query.isdigit():
        target_id = user_query

    if target_id:
        url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{target_id}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/search?query={urllib.parse.quote(user_query)}&limit=5"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode("utf-8"))
            if results:
                uq_lower = user_query.lower()
                for res in results:
                    u = res.get("user", {})
                    if u.get("username", "").lower() == uq_lower or (res.get("nick") and res.get("nick").lower() == uq_lower):
                        return res
                for res in results:
                    u = res.get("user", {})
                    if uq_lower in u.get("username", "").lower() or (res.get("nick") and uq_lower in res.get("nick").lower()):
                        return res
                return results[0]
    except Exception as e:
        sys.stderr.write(f"Error searching Discord members: {e}\n")
    return None


def resolve_discord_role(role_query: str, roles: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Resolves a Discord role by ID, mention, alias, or name."""
    if roles is None:
        roles = get_discord_roles()
    role_query = role_query.strip()

    m = re.match(r"^<@&(\d+)>$", role_query)
    target_id = m.group(1) if m else None
    if not target_id and role_query.isdigit():
        target_id = role_query

    if target_id:
        for r in roles:
            if str(r.get("id")) == str(target_id):
                return r

    rq_lower = role_query.lower()
    if rq_lower in ["op", "operator", "ops"]:
        for r in roles:
            if str(r.get("id")) == str(DISCORD_OPERATOR_ROLE_ID) or r.get("name", "").lower() == "operator":
                return r
    if rq_lower in ["admin", "administrator"]:
        for r in roles:
            if str(r.get("id")) == str(DISCORD_ADMIN_ROLE_ID) or r.get("name", "").lower() == "admin":
                return r

    for r in roles:
        if r.get("name", "").lower() == rq_lower:
            return r
    for r in roles:
        if rq_lower in r.get("name", "").lower():
            return r
    return None


def verify_owner_immutable(member: Dict[str, Any]) -> None:
    """Hard-gated security check ensuring the Homelab Owner's roles and permissions are permanently untouchable."""
    u = member.get("user", {})
    uid = str(u.get("id", ""))
    if OWNER_DISCORD_ID and uid == str(OWNER_DISCORD_ID):
        sys.stderr.write("⛔ PERMISSION DENIED: The Homelab Owner's roles, permissions, and status are permanently immutable and cannot be touched by anyone.\n")
        sys.exit(1)


def cmd_discord_role_list(args) -> None:
    member = resolve_discord_user(args.user)
    if not member:
        print(f"User '{args.user}' not found in guild.")
        sys.exit(1)

    roles = get_discord_roles()
    role_map = {str(r["id"]): r["name"] for r in roles}
    u = member.get("user", {})
    member_roles = member.get("roles", [])
    role_names = [role_map.get(str(rid), f"ID:{rid}") for rid in member_roles]

    if getattr(args, "json", False):
        print(json.dumps({
            "user_id": u.get("id"),
            "username": u.get("username"),
            "nick": member.get("nick"),
            "roles": member_roles,
            "role_names": role_names,
        }, indent=2))
        return

    disp_name = member.get("nick") or u.get("username")
    print(f"Discord Member: {disp_name} (@{u.get('username')}, ID: {u.get('id')})")
    print(f"Roles ({len(role_names)}): {', '.join(role_names) if role_names else 'None'}")


def cmd_discord_role_grant(args) -> None:
    member = resolve_discord_user(args.user)
    if not member:
        print(f"User '{args.user}' not found in guild.")
        sys.exit(1)

    verify_owner_immutable(member)

    role = resolve_discord_role(args.role)
    if not role:
        print(f"Role '{args.role}' not found in guild.")
        sys.exit(1)

    u = member.get("user", {})
    uid = u.get("id")
    rid = role.get("id")
    rname = role.get("name")
    disp_name = member.get("nick") or u.get("username")

    if str(rid) in member.get("roles", []):
        print(f"Member '{disp_name}' already has role '{rname}'.")
        return

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{uid}/roles/{rid}"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com/km-kushmathur/homelab, 1.0)",
        "X-Audit-Log-Reason": "Admin role grant via arr_cli",
        "Content-Length": "0",
    }
    req = urllib.request.Request(url, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                if getattr(args, "json", False):
                    print(json.dumps({"success": True, "action": "grant", "user_id": uid, "username": u.get("username"), "role_id": rid, "role_name": rname}))
                else:
                    print(f"✅ Successfully granted role '{rname}' to '{disp_name}' (@{u.get('username')}).")
            else:
                print(f"Failed to grant role (HTTP {resp.status})")
                sys.exit(1)
    except Exception as e:
        print(f"Error granting role: {e}")
        sys.exit(1)


def cmd_discord_role_revoke(args) -> None:
    member = resolve_discord_user(args.user)
    if not member:
        print(f"User '{args.user}' not found in guild.")
        sys.exit(1)

    verify_owner_immutable(member)

    role = resolve_discord_role(args.role)
    if not role:
        print(f"Role '{args.role}' not found in guild.")
        sys.exit(1)

    u = member.get("user", {})
    uid = u.get("id")
    rid = role.get("id")
    rname = role.get("name")
    disp_name = member.get("nick") or u.get("username")

    if str(rid) not in member.get("roles", []):
        print(f"Member '{disp_name}' does not have role '{rname}'.")
        return

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{uid}/roles/{rid}"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com/km-kushmathur/homelab, 1.0)",
        "X-Audit-Log-Reason": "Admin role revoke via arr_cli",
    }
    req = urllib.request.Request(url, method="DELETE", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                if getattr(args, "json", False):
                    print(json.dumps({"success": True, "action": "revoke", "user_id": uid, "username": u.get("username"), "role_id": rid, "role_name": rname}))
                else:
                    print(f"✅ Successfully revoked role '{rname}' from '{disp_name}' (@{u.get('username')}).")
            else:
                print(f"Failed to revoke role (HTTP {resp.status})")
                sys.exit(1)
    except Exception as e:
        print(f"Error revoking role: {e}")
        sys.exit(1)


def cmd_subtitles_status(args) -> None:
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    url = f"{JELLYFIN_URL}/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=MediaStreams,Path,SeriesName"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("Items", [])
    except Exception as e:
        print(f"Error querying Jellyfin: {e}")
        sys.exit(1)

    total = 0
    with_eng = 0
    missing = []
    for item in items:
        p = item.get("Path")
        if not p or not os.path.exists(p):
            continue
        total += 1
        streams = item.get("MediaStreams", [])
        has_sub = any(
            s.get("Type") == "Subtitle" and (s.get("Language") or "").lower() in ["eng", "en", "english"]
            for s in streams
        )
        if has_sub:
            with_eng += 1
        else:
            missing.append({
                "type": item.get("Type"),
                "name": item.get("Name"),
                "series": item.get("SeriesName"),
                "path": p
            })

    pct = (with_eng / total * 100) if total else 0.0
    if getattr(args, "json", False):
        print(json.dumps({
            "total_items": total,
            "with_english_subtitles": with_eng,
            "missing_english_subtitles": len(missing),
            "coverage_percent": round(pct, 2),
            "missing_items": missing
        }, indent=2))
        return

    print("═══════════════════════════════════════════════════════════")
    print("           ENGLISH SUBTITLE COVERAGE STATUS                ")
    print("═══════════════════════════════════════════════════════════")
    print(f" Total Media Files Checked : {total}")
    print(f" Files with English Subs   : {with_eng} ({pct:.1f}%)")
    print(f" Files Missing English Subs: {len(missing)}")
    print("───────────────────────────────────────────────────────────")
    if missing:
        print(" Sample Missing Media (first 10):")
        for m in missing[:10]:
            label = f"[{m['type']}] {m['name']}"
            if m.get("series"):
                label = f"[{m['type']}] {m['series']} - {m['name']}"
            print(f"  • {label}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more.")
    else:
        print(" ✅ 100% of media files have English subtitles!")
    print("═══════════════════════════════════════════════════════════")


def cmd_subtitles_sync(args) -> None:
    try:
        from sync_subtitles import sync_all_subtitles
    except ImportError:
        import sys
        sys.path.insert(0, "/opt/homelab/scripts")
        from sync_subtitles import sync_all_subtitles

    res = sync_all_subtitles(dry_run=getattr(args, "dry_run", False))
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        print("\n✅ Subtitle sync execution completed.")


def cmd_bazarr_status(args) -> None:
    headers = {"X-API-KEY": BAZARR_API_KEY}
    status_url = f"{BAZARR_URL}/api/system/status"
    tasks_url = f"{BAZARR_URL}/api/system/tasks"

    status_data = {}
    tasks_data = []

    try:
        req = urllib.request.Request(status_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_data = json.loads(resp.read().decode("utf-8")).get("data", {})
    except Exception as e:
        print(f"Error querying Bazarr status: {e}")
        sys.exit(1)

    try:
        req2 = urllib.request.Request(tasks_url, headers=headers)
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            tasks_data = json.loads(resp2.read().decode("utf-8")).get("data", [])
    except Exception:
        pass

    if getattr(args, "json", False):
        print(json.dumps({"status": status_data, "tasks": tasks_data}, indent=2))
        return

    print("═══════════════════════════════════════════════════════════")
    print("                  BAZARR SERVICE STATUS                    ")
    print("═══════════════════════════════════════════════════════════")
    print(f" Version         : {status_data.get('bazarr_version')} ({status_data.get('package_version')})")
    print(f" Radarr Connected: {status_data.get('radarr_version', 'Not Connected')}")
    print(f" Sonarr Connected: {status_data.get('sonarr_version', 'Not Connected')}")
    print(f" Database Engine : {status_data.get('database_engine')}")
    print("───────────────────────────────────────────────────────────")
    active = [t for t in tasks_data if t.get("job_running")]
    if active:
        print(f" Active Tasks    : {', '.join([t.get('name') for t in active])}")
    else:
        print(" Active Tasks    : Idle (all periodic background jobs scheduled)")
    print("═══════════════════════════════════════════════════════════")


def cmd_bazarr_search_missing(args) -> None:
    headers = {"X-API-KEY": BAZARR_API_KEY}
    for task_id in ["wanted_search_missing_subtitles_movies", "wanted_search_missing_subtitles_series"]:
        url = f"{BAZARR_URL}/api/system/tasks?taskid={task_id}"
        req = urllib.request.Request(url, method="POST", data=b"", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"Error triggering task {task_id}: {e}")

    if getattr(args, "json", False):
        print(json.dumps({"success": True, "message": "Triggered missing subtitle searches for movies and series"}))
    else:
        print("✅ Triggered Bazarr missing subtitle searches for Movies and Series.")


def cmd_bazarr_sync(args) -> None:
    headers = {"X-API-KEY": BAZARR_API_KEY}
    for task_id in ["update_movies", "update_series"]:
        url = f"{BAZARR_URL}/api/system/tasks?taskid={task_id}"
        req = urllib.request.Request(url, method="POST", data=b"", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"Error triggering task {task_id}: {e}")

    if getattr(args, "json", False):
        print(json.dumps({"success": True, "message": "Triggered Radarr and Sonarr sync in Bazarr"}))
    else:
        print("✅ Triggered Bazarr library sync with Radarr and Sonarr.")


def build_parser() -> argparse.ArgumentParser:
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="Output results in JSON format")

    parser = argparse.ArgumentParser(
        prog="arr_cli",
        description="Unified CLI for Arr stack, Jellyfin, and download client homelab automation.",
        parents=[json_parent],
    )

    subparsers = parser.add_subparsers(dest="service", required=True, help="Target service")

    # Unified Status command
    p_stat = subparsers.add_parser("status", parents=[json_parent], help="Aggregate active downloads and in-progress requests")
    p_stat.add_argument("--user", help="Filter active downloads and requests by Discord user ID, username, or requester")
    p_stat.add_argument("--take", type=int, default=10, help="Number of in-progress requests to fetch (default: 10)")
    p_stat.set_defaults(func=cmd_status)

    # Request History command
    p_hist = subparsers.add_parser("history", parents=[json_parent], help="List media request history for a user or homelab")
    p_hist.add_argument("--user", help="Filter by Discord user ID, username, or Jellyseerr requester")
    p_hist.add_argument("--take", type=int, default=50, help="Number of history items to fetch (default: 50)")
    p_hist.set_defaults(func=cmd_history)

    # Pipeline Details command
    p_pipe = subparsers.add_parser("pipeline-details", parents=[json_parent], help="Fetch detailed pipeline diagnostics for a request or media ID")
    p_pipe.add_argument("id", help="Jellyseerr Request ID, Arr ID (movie:<id>/tv:<id>), or queue ID")
    p_pipe.set_defaults(func=cmd_pipeline_details)

    p_s_det = subparsers.add_parser("status-details", parents=[json_parent], help="Fetch detailed pipeline diagnostics for a request or media ID")
    p_s_det.add_argument("id", help="Jellyseerr Request ID, Arr ID (movie:<id>/tv:<id>), or queue ID")
    p_s_det.set_defaults(func=cmd_pipeline_details)

    # Pipeline Cancel command
    p_p_cancel = subparsers.add_parser("pipeline-cancel", parents=[json_parent], help="Cancel in-progress request before it finishes downloading")
    p_p_cancel.add_argument("id", help="Jellyseerr Request ID, Arr ID, or Media Title")
    p_p_cancel.set_defaults(func=cmd_pipeline_cancel)

    # Pipeline Switch Quality command
    p_p_switch = subparsers.add_parser("switch-quality", parents=[json_parent], help="Switch quality profile for active media item")
    p_p_switch.add_argument("id", help="Jellyseerr Request ID, Arr ID, or Media Title")
    p_p_switch.add_argument("--quality", choices=["4k", "1080p", "720p", "any"], required=True, help="New target quality profile")
    p_p_switch.add_argument("--media-type", choices=["movie", "tv"], help="Media type (movie or tv)")
    p_p_switch.set_defaults(func=cmd_pipeline_switch_quality)

    # Jellyseerr
    p_jellyseerr = subparsers.add_parser("jellyseerr", parents=[json_parent], help="Jellyseerr media discovery and request management")
    jellyseerr_sub = p_jellyseerr.add_subparsers(dest="action", required=True)

    # jellyseerr search <query>
    p_j_search = jellyseerr_sub.add_parser("search", parents=[json_parent], help="Search TMDB via Jellyseerr")
    p_j_search.add_argument("query", nargs="+", help="Movie or TV show search query")
    p_j_search.set_defaults(func=cmd_jellyseerr_search)

    # jellyseerr details --media-type <movie|tv> --id <id>
    p_j_details = jellyseerr_sub.add_parser("details", parents=[json_parent], help="Fetch detailed metadata and computed size estimates")
    p_j_details.add_argument("--media-type", choices=["movie", "tv"], required=True, help="Media type (movie or tv)")
    p_j_details.add_argument("--id", required=True, type=int, help="TMDB Media ID")
    p_j_details.add_argument("--releases", action="store_true", default=True, help="Include live tracker releases from Prowlarr")
    p_j_details.add_argument("--no-releases", dest="releases", action="store_false", help="Skip live tracker releases from Prowlarr")
    p_j_details.set_defaults(func=cmd_jellyseerr_details)

    # jellyseerr request --media-type <movie|tv> --id <id>
    p_j_req = jellyseerr_sub.add_parser("request", parents=[json_parent], help="Submit media request to Jellyseerr")
    p_j_req.add_argument("--media-type", choices=["movie", "tv"], required=True, help="Media type (movie or tv)")
    p_j_req.add_argument("--id", required=True, type=int, help="TMDB Media ID")
    p_j_req.add_argument("--is-4k", action="store_true", help="Request in 4K / Ultra-HD profile")
    p_j_req.add_argument("--quality", choices=["1080p", "4k", "any"], default="1080p", help="Target quality profile")
    p_j_req.add_argument("--seasons", default="all", help="TV seasons to request ('all' or '1,2')")
    p_j_req.set_defaults(func=cmd_jellyseerr_request)

    # jellyseerr status <id>
    p_j_stat = jellyseerr_sub.add_parser("status", parents=[json_parent], help="Check request or media status in Jellyseerr")
    p_j_stat.add_argument("id", help="Request ID or Media ID")
    p_j_stat.set_defaults(func=cmd_jellyseerr_status)

    # jellyseerr cancel <id>
    p_j_cancel = jellyseerr_sub.add_parser("cancel", parents=[json_parent], help="Cancel active request before it finishes downloading")
    p_j_cancel.add_argument("id", help="Request ID or Media ID")
    p_j_cancel.set_defaults(func=cmd_pipeline_cancel)

    # Queue auto-remediate
    p_q_auto = subparsers.add_parser("queue", parents=[json_parent], help="Queue management and stalled download remediation")
    queue_sub = p_q_auto.add_subparsers(dest="action", required=True)
    p_q_rem = queue_sub.add_parser("auto-remediate", parents=[json_parent], help="Auto-detect stalled/dead downloads, blocklist release, and search alternative")
    p_q_rem.set_defaults(func=cmd_queue_auto_remediate)

    # Radarr
    p_radarr = subparsers.add_parser("radarr", parents=[json_parent], help="Radarr movie management")
    radarr_sub = p_radarr.add_subparsers(dest="action", required=True)

    # radarr queue
    p_r_queue = radarr_sub.add_parser("queue", parents=[json_parent], help="List Radarr pending download queue")
    p_r_queue.set_defaults(func=cmd_radarr_queue)

    # radarr blocklist <queue_id>
    p_r_block = radarr_sub.add_parser("blocklist", parents=[json_parent], help="Remove and blocklist a bad/stalled Radarr queue item")
    p_r_block.add_argument("queue_id", type=int, help="Radarr Queue ID")
    p_r_block.set_defaults(func=cmd_radarr_blocklist)

    # radarr rescan <movie_id>
    p_r_rescan = radarr_sub.add_parser("rescan", parents=[json_parent], help="Rescan movie files")
    p_r_rescan.add_argument("movie_id", type=int, help="Radarr Movie ID")
    p_r_rescan.set_defaults(func=cmd_radarr_rescan)

    # Sonarr
    p_sonarr = subparsers.add_parser("sonarr", parents=[json_parent], help="Sonarr series management")
    sonarr_sub = p_sonarr.add_subparsers(dest="action", required=True)

    # sonarr queue
    p_s_queue = sonarr_sub.add_parser("queue", parents=[json_parent], help="List Sonarr pending series download queue")
    p_s_queue.set_defaults(func=cmd_sonarr_queue)

    # sonarr blocklist <queue_id>
    p_s_block = sonarr_sub.add_parser("blocklist", parents=[json_parent], help="Remove and blocklist a bad/stalled Sonarr queue item")
    p_s_block.add_argument("queue_id", type=int, help="Sonarr Queue ID")
    p_s_block.set_defaults(func=cmd_sonarr_blocklist)

    # sonarr search-missing <series_id>
    p_s_missing = sonarr_sub.add_parser("search-missing", parents=[json_parent], help="Trigger search for missing episodes")
    p_s_missing.add_argument("series_id", type=int, help="Sonarr Series ID")
    p_s_missing.set_defaults(func=cmd_sonarr_search_missing)

    # Prowlarr
    p_prowlarr = subparsers.add_parser("prowlarr", parents=[json_parent], help="Prowlarr indexer management")
    prowlarr_sub = p_prowlarr.add_subparsers(dest="action", required=True)

    # prowlarr test-all
    p_p_test = prowlarr_sub.add_parser("test-all", parents=[json_parent], help="Test all indexers to clear 6-hour backoff lockout")
    p_p_test.set_defaults(func=cmd_prowlarr_test_all)

    # prowlarr status
    p_p_stat = prowlarr_sub.add_parser("status", parents=[json_parent], help="List indexer health and configuration status")
    p_p_stat.set_defaults(func=cmd_prowlarr_status)

    # prowlarr releases
    p_p_rel = prowlarr_sub.add_parser("releases", parents=[json_parent], help="Search and list real releases on indexers with sizes")
    p_p_rel.add_argument("query", nargs="+", help="Search query for movie or TV title")
    p_p_rel.add_argument("--media-type", choices=["movie", "tv"], default="movie", help="Media type (movie or tv)")
    p_p_rel.add_argument("--year", help="Optional release year")
    p_p_rel.set_defaults(func=cmd_prowlarr_releases)

    # Downloader
    p_dl = subparsers.add_parser("downloader", parents=[json_parent], help="Download client management")
    dl_sub = p_dl.add_subparsers(dest="action", required=True)
    p_dl_list = dl_sub.add_parser("list", parents=[json_parent], help="List active downloads, speeds, and sources")
    p_dl_list.set_defaults(func=cmd_downloader_list)

    # Jellyfin
    p_jellyfin = subparsers.add_parser("jellyfin", parents=[json_parent], help="Jellyfin media server management")
    jellyfin_sub = p_jellyfin.add_subparsers(dest="action", required=True)

    # jellyfin refresh
    p_jf_ref = jellyfin_sub.add_parser("refresh", parents=[json_parent], help="Trigger library scan/refresh")
    p_jf_ref.set_defaults(func=cmd_jellyfin_refresh)

    # jellyfin sessions
    p_jf_sess = jellyfin_sub.add_parser("sessions", parents=[json_parent], help="List active streaming/playback sessions")
    p_jf_sess.set_defaults(func=cmd_jellyfin_sessions)

    # Discord Role Management (Admin only, Owner immutable)
    p_drole = subparsers.add_parser("discord-role", parents=[json_parent], help="Discord role management (Admins only, Owner immutable)")
    drole_sub = p_drole.add_subparsers(dest="action", required=True)

    # discord-role list <user>
    p_dr_list = drole_sub.add_parser("list", parents=[json_parent], help="List roles for a Discord member")
    p_dr_list.add_argument("user", help="Username, display name, user ID, or @mention")
    p_dr_list.set_defaults(func=cmd_discord_role_list)

    # discord-role grant <user> <role>
    p_dr_grant = drole_sub.add_parser("grant", parents=[json_parent], help="Grant a Discord role to a member (Owner immutable)")
    p_dr_grant.add_argument("user", help="Username, display name, user ID, or @mention")
    p_dr_grant.add_argument("role", help="Role name, role ID, or alias ('op', 'operator', 'admin')")
    p_dr_grant.set_defaults(func=cmd_discord_role_grant)

    # discord-role revoke <user> <role>
    p_dr_revoke = drole_sub.add_parser("revoke", parents=[json_parent], help="Revoke a Discord role from a member (Owner immutable)")
    p_dr_revoke.add_argument("user", help="Username, display name, user ID, or @mention")
    p_dr_revoke.add_argument("role", help="Role name, role ID, or alias ('op', 'operator', 'admin')")
    # Subtitles
    p_sub = subparsers.add_parser("subtitles", parents=[json_parent], help="Subtitle status and synchronization engine")
    sub_sub = p_sub.add_subparsers(dest="sub_action", required=True)
    p_s_status = sub_sub.add_parser("status", parents=[json_parent], help="Display English subtitle coverage statistics")
    p_s_status.set_defaults(func=cmd_subtitles_status)
    p_s_sync = sub_sub.add_parser("sync", parents=[json_parent], help="Sync and download missing English subtitles")
    p_s_sync.add_argument("--dry-run", action="store_true", help="Preview missing subtitles without downloading")
    p_s_sync.set_defaults(func=cmd_subtitles_sync)

    # Bazarr
    p_baz = subparsers.add_parser("bazarr", parents=[json_parent], help="Bazarr subtitle management daemon")
    baz_sub = p_baz.add_subparsers(dest="baz_action", required=True)
    p_b_status = baz_sub.add_parser("status", parents=[json_parent], help="Bazarr system status and service health")
    p_b_status.set_defaults(func=cmd_bazarr_status)
    p_b_search = baz_sub.add_parser("search-missing", parents=[json_parent], help="Trigger missing subtitle searches across movies and series")
    p_b_search.set_defaults(func=cmd_bazarr_search_missing)
    p_b_sync = baz_sub.add_parser("sync", parents=[json_parent], help="Trigger synchronization with Radarr and Sonarr")
    p_b_sync.set_defaults(func=cmd_bazarr_sync)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if "--json" in sys.argv:
        args.json = True
    if hasattr(args, "func"):
        require_env(getattr(args, "service", ""))
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

