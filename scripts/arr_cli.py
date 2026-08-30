#!/usr/bin/env python3
"""
Unified CLI for the homelab media stack.

Wraps Jellyseerr, Radarr, Sonarr, Prowlarr, the download client, and Jellyfin
behind one command surface, so the Discord bridge, the SRE agent, and a human
at a shell all drive the stack through the same code path.

Configuration comes entirely from the environment. Copy
scripts/homelab.env.example to /etc/homelab/homelab.env (mode 0600) and fill it
in; override the location with HOMELAB_ENV_FILE. Nothing here has a credential
baked in as a fallback -- a missing value produces an error naming the variable.
"""

import os
import sys
import re
import json
import argparse
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Union

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

# Service endpoints. The defaults below match the addressing used throughout
# docs/ and exist only so `--help` and dry runs work on a fresh checkout;
# set them explicitly for your own network.
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

# Credentials each command group needs in order to do anything useful.
# Endpoints are not listed: those have documented defaults above and a wrong
# URL fails loudly on its own. A missing API key, by contrast, surfaces as an
# opaque HTTP 401 -- so it is worth catching before the first request.
REQUIRED_ENV: Dict[str, List[str]] = {
    "jellyseerr": ["JELLYSEERR_API_KEY"],
    "radarr": ["RADARR_API_KEY"],
    "sonarr": ["SONARR_API_KEY"],
    "prowlarr": ["PROWLARR_API_KEY"],
    "jellyfin": ["JELLYFIN_API_KEY"],
    "downloader": [],
    "queue": ["RADARR_API_KEY", "SONARR_API_KEY"],
    "status": ["RADARR_API_KEY", "SONARR_API_KEY"],
    "pipeline-details": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
    "status-details": ["JELLYSEERR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"],
}


def require_env(group: str) -> None:
    """Exits with a clear message if the variables a command group needs are unset."""
    missing = [name for name in REQUIRED_ENV.get(group, []) if not os.environ.get(name)]
    if not missing:
        return
    env_file = os.environ.get("HOMELAB_ENV_FILE", DEFAULT_ENV_FILE)
    sys.stderr.write(
        "Missing credentials for '%s': %s\n"
        "Set these in %s (see scripts/homelab.env.example) or in the environment.\n"
        % (group, ", ".join(missing), env_file)
    )
    sys.exit(2)


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
    Example: 'example movie 4k' -> clean_title: 'example movie', quality: '4k'
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
    return {"quality_label": label, "quality_key": q_key, "resolution": res, "source": source}


def fetch_prowlarr_releases(title: str, media_type: str = "movie", year: Optional[str] = None, timeout: int = 40) -> Dict[str, Any]:
    """
    Queries Prowlarr for real tracker releases and provides an interactive size and quality breakdown.
    """
    category = 2000 if media_type == "movie" else 5000
    enc = urllib.parse.quote(title)
    url = f"{PROWLARR_URL}/api/v1/search?query={enc}&type=search&categories={category}"
    res = make_request(url, headers={"X-Api-Key": PROWLARR_API_KEY}, timeout=timeout)

    if not isinstance(res, list) or not res:
        return {"has_releases": False, "total_releases": 0, "qualities": {}, "top_releases": []}

    qualities: Dict[str, List[Dict[str, Any]]] = {"4k": [], "1080p": [], "720p": [], "other": []}
    top_releases: List[Dict[str, Any]] = []

    for item in res:
        t = item.get("title", "")
        sz_bytes = item.get("size", 0) or 0
        sz_gb = round(sz_bytes / (1024.0**3), 2)
        seeds = item.get("seeders", 0) or 0
        idx = item.get("indexer", "Tracker")
        q_meta = parse_release_quality(t)
        q_key = q_meta["quality_key"]

        if sz_gb > 0:
            qualities[q_key].append({
                "size_gb": sz_gb,
                "title": t,
                "seeders": seeds,
                "indexer": idx,
                "quality_label": q_meta["quality_label"],
                "quality_key": q_key,
            })

    summary: Dict[str, Any] = {}
    for q_key in ["4k", "1080p", "720p", "other"]:
        items = qualities.get(q_key, [])
        if items:
            sizes = [x["size_gb"] for x in items]
            best_by_seeds = sorted(items, key=lambda x: x["seeders"], reverse=True)[0]
            summary[q_key] = {
                "count": len(items),
                "min_gb": min(sizes),
                "max_gb": max(sizes),
                "avg_gb": round(sum(sizes) / len(sizes), 1),
                "best_release": best_by_seeds,
            }

    sorted_all = sorted(res, key=lambda x: x.get("seeders", 0) or 0, reverse=True)[:10]
    for idx_num, x in enumerate(sorted_all):
        t = x.get("title", "Unknown")
        q_meta = parse_release_quality(t)
        top_releases.append({
            "idx": idx_num,
            "title": t,
            "size_gb": round((x.get("size", 0) or 0) / (1024.0**3), 2),
            "seeders": x.get("seeders", 0) or 0,
            "indexer": x.get("indexer", "Tracker"),
            "quality_label": q_meta["quality_label"],
            "quality_key": q_meta["quality_key"],
            "guid": x.get("guid") or x.get("downloadUrl") or "",
        })

    return {
        "has_releases": True,
        "total_releases": len(res),
        "qualities": summary,
        "top_releases": top_releases,
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

    payload = {
        "mediaType": media_type,
        "mediaId": media_id,
        "is4k": is_4k,
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
    Jellyseerr -> Radarr/Sonarr -> Downloader -> Storage Import -> Jellyfin.
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
                arr_id = media_data.get("externalServiceId")

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
                tmdb_id = arr_data.get("tvdbId")

    # If still no req_data, try matching Jellyseerr request by TMDB/Arr ID
    if not req_data and tmdb_id:
        j_all = make_request(f"{JELLYSEERR_URL}/api/v1/request?take=50&filter=all", headers={"X-Api-Key": JELLYSEERR_API_KEY})
        for r in (j_all.get("results", []) if isinstance(j_all, dict) else []):
            m = r.get("media") or {}
            if m.get("tmdbId") == tmdb_id or (arr_id and m.get("externalServiceId") == arr_id):
                req_data = r
                req_id = r.get("id")
                media_data = m
                if not mtype:
                    mtype = r.get("type", "movie")
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
    downloader_info: Optional[Dict[str, Any]] = None
    if queue_item and queue_item.get("downloadId"):
        dl_id = queue_item.get("downloadId", "").lower()
        dl_endpoint = os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info")
        downloads = make_request(f"{DOWNLOADER_URL}{dl_endpoint}")
        if isinstance(downloads, list):
            for d in downloads:
                if (d.get("hash") or d.get("id", "")).lower() == dl_id:
                    downloader_info = d
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
        useful_info["indexer"] = queue_item.get("indexer", "Prowlarr Indexer")

        if downloader_info:
            useful_info["dlspeed"] = format_speed(downloader_info.get("dlspeed", 0))
            useful_info["upspeed"] = format_speed(downloader_info.get("upspeed", 0))
            seeds_val = downloader_info.get("num_seeds", 0)
            comp_val = downloader_info.get("num_complete", 0)
            useful_info["seeds"] = f"{seeds_val} connected ({comp_val} total)"
            useful_info["state"] = downloader_info.get("state", "downloading")

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
        progress_context = "Awaiting matching indexer release (0%)"
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
        "downloader_info": downloader_info,
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
    print(f"🟡 Jellyseerr:        Req #{data.get('id')} by {data.get('requested_by')} • Profile: {data.get('profile_name')} ({data.get('season_str')})")
    arr_stat = "Monitored: Yes" if (data.get("arr_data") or {}).get("monitored") else "Monitored: No"
    arr_path = f" • Path: {data.get('library_path')}" if data.get("library_path") else ""
    print(f"🟣 {data.get('arr_service')}:            ID #{data.get('arr_id')} ({arr_stat}){arr_path}")

    u_info = data.get("useful_info", {})
    if u_info:
        dl_t = u_info.get("download_title") or "N/A"
        print(f"🔵 Downloader:        {dl_t}")
        print(f"   Speeds:            {u_info.get('dlspeed', '0 B/s')} ↓ / {u_info.get('upspeed', '0 B/s')} ↑ • ETA: {u_info.get('timeleft', 'N/A')}")
        print(f"   Sources:           {u_info.get('seeds', 'N/A')} • Indexer: {u_info.get('indexer', 'N/A')}")

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


def cmd_status(args: argparse.Namespace) -> None:
    """Aggregates active downloads from Radarr/Sonarr and ONLY IN-PROGRESS requests from Jellyseerr."""
    take_count = getattr(args, "take", 10) or 10

    # 1. Radarr Queue
    r_res = make_request(f"{RADARR_URL}/api/v3/queue", headers={"X-Api-Key": RADARR_API_KEY})
    r_queue = r_res.get("records", []) if isinstance(r_res, dict) else []

    # 2. Sonarr Queue
    s_res = make_request(f"{SONARR_URL}/api/v3/queue", headers={"X-Api-Key": SONARR_API_KEY})
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

    # Build structured active downloads
    downloads = []
    for item in r_queue:
        total_sz = item.get("size", 1) or 1
        sz_left = item.get("sizeleft", 0)
        pct = max(0.0, min(100.0, ((total_sz - sz_left) / total_sz) * 100.0))
        downloads.append({
            "type": "movie",
            "id": item.get("id"),
            "movie_id": item.get("movieId"),
            "title": item.get("title", "Unknown"),
            "status": item.get("status", "downloading"),
            "size": total_sz,
            "size_str": human_size(total_sz),
            "sizeleft": sz_left,
            "sizeleft_str": human_size(sz_left),
            "progress_pct": round(pct, 1),
            "progress_bar": make_progress_bar(pct),
            "timeleft": item.get("timeleft", "N/A"),
        })

    for item in s_queue:
        series_title = (item.get("series", {}) if isinstance(item.get("series"), dict) else {}).get("title") or item.get("title", "Unknown")
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
            "series_id": item.get("seriesId"),
            "title": f"{series_title} {ep_str}".strip(),
            "status": item.get("status", "downloading"),
            "size": total_sz,
            "size_str": human_size(total_sz),
            "sizeleft": sz_left,
            "sizeleft_str": human_size(sz_left),
            "progress_pct": round(pct, 1),
            "progress_bar": make_progress_bar(pct),
            "timeleft": item.get("timeleft", "N/A"),
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

        user_name = (item.get("requestedBy", {}) if isinstance(item.get("requestedBy"), dict) else {}).get("displayName", "Unknown")
        ext_service_id = media_obj.get("externalServiceId")

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


def cmd_queue_auto_remediate(args: argparse.Namespace) -> None:
    """
    Scans Radarr and Sonarr queues for stalled/dead downloads (0 sources, stalled status, warning flags),
    blocklists the dead release, removes it from the download client, and triggers a clean search for an alternative release.
    """
    remediated = []

    # 1. Check Radarr
    radarr_res = make_request(f"{RADARR_URL}/api/v3/queue?includeUnknownMovieItems=true", headers={"X-Api-Key": RADARR_API_KEY})
    radarr_records = radarr_res.get("records", []) if isinstance(radarr_res, dict) else []
    for item in radarr_records:
        q_id = item.get("id")
        title = item.get("title", "Unknown")
        status = (item.get("status") or "").lower()
        tracked_status = (item.get("trackedDownloadStatus") or "").lower()
        status_messages = item.get("statusMessages", [])

        is_stalled = False
        reason = ""

        if tracked_status in ["warning", "error"]:
            is_stalled = True
            msg_texts = [m.get("title", "") for m in status_messages if isinstance(m, dict)]
            reason = "; ".join(msg_texts) or f"Tracked status: {tracked_status}"
        elif status in ["warning", "stalled"]:
            is_stalled = True
            reason = f"Status is {status}"

        if is_stalled and q_id:
            del_url = f"{RADARR_URL}/api/v3/queue/{q_id}?removeFromClient=true&blocklist=true"
            make_request(del_url, method="DELETE", headers={"X-Api-Key": RADARR_API_KEY})
            movie_id = item.get("movieId")
            if movie_id:
                make_request(f"{RADARR_URL}/api/v3/command", method="POST", headers={"X-Api-Key": RADARR_API_KEY}, data={"name": "MoviesSearch", "movieIds": [movie_id]})
            remediated.append({
                "type": "movie",
                "title": title,
                "queue_id": q_id,
                "reason": reason,
                "action": "blocklisted_and_re-searched",
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

        is_stalled = False
        reason = ""

        if tracked_status in ["warning", "error"]:
            is_stalled = True
            msg_texts = [m.get("title", "") for m in status_messages if isinstance(m, dict)]
            reason = "; ".join(msg_texts) or f"Tracked status: {tracked_status}"
        elif status in ["warning", "stalled"]:
            is_stalled = True
            reason = f"Status is {status}"

        if is_stalled and q_id:
            del_url = f"{SONARR_URL}/api/v3/queue/{q_id}?removeFromClient=true&blocklist=true"
            make_request(del_url, method="DELETE", headers={"X-Api-Key": SONARR_API_KEY})
            series_id = item.get("seriesId")
            if series_id:
                make_request(f"{SONARR_URL}/api/v3/command", method="POST", headers={"X-Api-Key": SONARR_API_KEY}, data={"name": "SeriesSearch", "seriesId": series_id})
            remediated.append({
                "type": "tv",
                "title": title,
                "queue_id": q_id,
                "reason": reason,
                "action": "blocklisted_and_re-searched",
            })

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
        protocol = item.get("protocol", "standard")
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
            print(f"  Best seeded: {best.get('title')} ({best.get('size_gb')} GB, {best.get('seeders')} seeds) via {best.get('indexer')}")

    print("\nTop Seeded Releases:")
    fmt = "{:<8} {:<10} {:<20} {:<45}"
    print(fmt.format("SEEDS", "SIZE", "INDEXER", "RELEASE TITLE"))
    print("-" * 88)
    for r in data.get("top_releases", []):
        disp_t = (r.get("title", "")[:42] + "...") if len(r.get("title", "")) > 45 else r.get("title", "")
        print(fmt.format(str(r.get("seeders", 0)), f"{r.get('size_gb', 0)} GB", r.get("indexer", "")[:18], disp_t))


# --- Downloader Commands ---

def cmd_downloader_list(args: argparse.Namespace) -> None:
    dl_endpoint = os.environ.get("DOWNLOADER_INFO_ENDPOINT", "/api/v2/downloads/info")
    url = f"{DOWNLOADER_URL}{dl_endpoint}"
    res = make_request(url)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    if isinstance(res, dict) and res.get("error"):
        print(f"Error querying download client: {res.get('message') or res.get('response')}")
        sys.exit(1)

    items = res if isinstance(res, list) else []
    if not items:
        print("No active downloads in client.")
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
        seeds = f"{t.get('num_seeds', 0)} ({t.get('num_complete', 0)})"
        size = human_size(t.get("size", 0))

        disp_name = (name[:29] + "...") if len(name) > 32 else name
        print(fmt.format(disp_name, state, progress, dlspeed, upspeed, seeds, size))


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arr_cli",
        description="Unified CLI for the homelab media stack (Jellyseerr, Radarr, Sonarr, Prowlarr, download client, Jellyfin)."
    )
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    subparsers = parser.add_subparsers(dest="service", required=True, help="Target service")

    # Unified Status command
    p_stat = subparsers.add_parser("status", help="Aggregate active downloads and in-progress requests")
    p_stat.add_argument("--take", type=int, default=10, help="Number of in-progress requests to fetch (default: 10)")
    p_stat.set_defaults(func=cmd_status)

    # Pipeline Details command
    p_pipe = subparsers.add_parser("pipeline-details", help="Fetch detailed pipeline diagnostics for a request or media ID")
    p_pipe.add_argument("id", help="Jellyseerr Request ID, Arr ID (movie:<id>/tv:<id>), or queue ID")
    p_pipe.set_defaults(func=cmd_pipeline_details)

    p_s_det = subparsers.add_parser("status-details", help="Fetch detailed pipeline diagnostics for a request or media ID")
    p_s_det.add_argument("id", help="Jellyseerr Request ID, Arr ID (movie:<id>/tv:<id>), or queue ID")
    p_s_det.set_defaults(func=cmd_pipeline_details)

    # Jellyseerr
    p_jellyseerr = subparsers.add_parser("jellyseerr", help="Jellyseerr media discovery and request management")
    jellyseerr_sub = p_jellyseerr.add_subparsers(dest="action", required=True)

    # jellyseerr search <query>
    p_j_search = jellyseerr_sub.add_parser("search", help="Search TMDB via Jellyseerr")
    p_j_search.add_argument("query", nargs="+", help="Movie or TV show search query")
    p_j_search.set_defaults(func=cmd_jellyseerr_search)

    # jellyseerr details --media-type <movie|tv> --id <id>
    p_j_details = jellyseerr_sub.add_parser("details", help="Fetch detailed metadata and computed size estimates")
    p_j_details.add_argument("--media-type", choices=["movie", "tv"], required=True, help="Media type (movie or tv)")
    p_j_details.add_argument("--id", required=True, type=int, help="TMDB Media ID")
    p_j_details.add_argument("--releases", action="store_true", default=True, help="Include live tracker releases from Prowlarr")
    p_j_details.add_argument("--no-releases", dest="releases", action="store_false", help="Skip live tracker releases from Prowlarr")
    p_j_details.set_defaults(func=cmd_jellyseerr_details)

    # jellyseerr request --media-type <movie|tv> --id <id>
    p_j_req = jellyseerr_sub.add_parser("request", help="Submit media request to Jellyseerr")
    p_j_req.add_argument("--media-type", choices=["movie", "tv"], required=True, help="Media type (movie or tv)")
    p_j_req.add_argument("--id", required=True, type=int, help="TMDB Media ID")
    p_j_req.add_argument("--is-4k", action="store_true", help="Request in 4K / Ultra-HD profile")
    p_j_req.add_argument("--quality", choices=["1080p", "4k", "any"], default="1080p", help="Target quality profile")
    p_j_req.add_argument("--seasons", default="all", help="TV seasons to request ('all' or '1,2')")
    p_j_req.set_defaults(func=cmd_jellyseerr_request)

    # jellyseerr status <id>
    p_j_stat = jellyseerr_sub.add_parser("status", help="Check request or media status in Jellyseerr")
    p_j_stat.add_argument("id", help="Request ID or Media ID")
    p_j_stat.set_defaults(func=cmd_jellyseerr_status)

    # Queue auto-remediate
    p_q_auto = subparsers.add_parser("queue", help="Queue management and stalled download remediation")
    queue_sub = p_q_auto.add_subparsers(dest="action", required=True)
    p_q_rem = queue_sub.add_parser("auto-remediate", help="Auto-detect stalled/dead downloads, blocklist release, and search alternative")
    p_q_rem.set_defaults(func=cmd_queue_auto_remediate)

    # Radarr
    p_radarr = subparsers.add_parser("radarr", help="Radarr movie management")
    radarr_sub = p_radarr.add_subparsers(dest="action", required=True)

    # radarr queue
    p_r_queue = radarr_sub.add_parser("queue", help="List Radarr pending download queue")
    p_r_queue.set_defaults(func=cmd_radarr_queue)

    # radarr blocklist <queue_id>
    p_r_block = radarr_sub.add_parser("blocklist", help="Remove and blocklist a bad/stalled Radarr queue item")
    p_r_block.add_argument("queue_id", type=int, help="Radarr Queue ID")
    p_r_block.set_defaults(func=cmd_radarr_blocklist)

    # radarr rescan <movie_id>
    p_r_rescan = radarr_sub.add_parser("rescan", help="Rescan movie files")
    p_r_rescan.add_argument("movie_id", type=int, help="Radarr Movie ID")
    p_r_rescan.set_defaults(func=cmd_radarr_rescan)

    # Sonarr
    p_sonarr = subparsers.add_parser("sonarr", help="Sonarr series management")
    sonarr_sub = p_sonarr.add_subparsers(dest="action", required=True)

    # sonarr queue
    p_s_queue = sonarr_sub.add_parser("queue", help="List Sonarr pending series download queue")
    p_s_queue.set_defaults(func=cmd_sonarr_queue)

    # sonarr blocklist <queue_id>
    p_s_block = sonarr_sub.add_parser("blocklist", help="Remove and blocklist a bad/stalled Sonarr queue item")
    p_s_block.add_argument("queue_id", type=int, help="Sonarr Queue ID")
    p_s_block.set_defaults(func=cmd_sonarr_blocklist)

    # sonarr search-missing <series_id>
    p_s_missing = sonarr_sub.add_parser("search-missing", help="Trigger search for missing episodes")
    p_s_missing.add_argument("series_id", type=int, help="Sonarr Series ID")
    p_s_missing.set_defaults(func=cmd_sonarr_search_missing)

    # Prowlarr
    p_prowlarr = subparsers.add_parser("prowlarr", help="Prowlarr indexer management")
    prowlarr_sub = p_prowlarr.add_subparsers(dest="action", required=True)

    # prowlarr test-all
    p_p_test = prowlarr_sub.add_parser("test-all", help="Test all indexers to clear 6-hour backoff lockout")
    p_p_test.set_defaults(func=cmd_prowlarr_test_all)

    # prowlarr status
    p_p_stat = prowlarr_sub.add_parser("status", help="List indexer health and configuration status")
    p_p_stat.set_defaults(func=cmd_prowlarr_status)

    # prowlarr releases
    p_p_rel = prowlarr_sub.add_parser("releases", help="Search and list real releases on indexers with sizes")
    p_p_rel.add_argument("query", nargs="+", help="Search query for movie or TV title")
    p_p_rel.add_argument("--media-type", choices=["movie", "tv"], default="movie", help="Media type (movie or tv)")
    p_p_rel.add_argument("--year", help="Optional release year")
    p_p_rel.set_defaults(func=cmd_prowlarr_releases)

    # Downloader
    p_dl = subparsers.add_parser("downloader", help="Download client management")
    dl_sub = p_dl.add_subparsers(dest="action", required=True)

    # downloader list
    p_dl_list = dl_sub.add_parser("list", help="List active downloads, speeds, and sources")
    p_dl_list.set_defaults(func=cmd_downloader_list)

    # Jellyfin
    p_jellyfin = subparsers.add_parser("jellyfin", help="Jellyfin media server management")
    jellyfin_sub = p_jellyfin.add_subparsers(dest="action", required=True)

    # jellyfin refresh
    p_jf_ref = jellyfin_sub.add_parser("refresh", help="Trigger library scan/refresh")
    p_jf_ref.set_defaults(func=cmd_jellyfin_refresh)

    # jellyfin sessions
    p_jf_sess = jellyfin_sub.add_parser("sessions", help="List active streaming/playback sessions")
    p_jf_sess.set_defaults(func=cmd_jellyfin_sessions)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return
    require_env(getattr(args, "service", ""))
    args.func(args)


if __name__ == "__main__":
    main()
