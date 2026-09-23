# Media Requests Guide

Guide for requesting media via Discord or the Jellyseerr web interface.

---

## Discord Requests

1. **Request channel:** Navigate to `#req-<username>`. If the channel is not present, execute `/open-channel` in any channel to create it.
2. **Search and request:**
   ```text
   /request <title> [quality]
   ```
   Examples:
   - `/request example title` — Searches for the title with standard 1080p profile.
   - `/request example title 4k` — Preselects 4K profile.
   - `/request example series season 2` — Preselects Season 2 for a series.
3. **Verify size:** Review the estimated storage footprint displayed on the response embed. Select **`🔍 Interactive Release Search`** to query available indexer releases with exact sizes.
4. **Confirm:** Select **`✅ Confirm Download`** to queue the download.
5. **Track status:** Run `/status` to view active download progress bars and estimated completion times. Select **`🔍 See Details`** for transfer rates and direct playback links.

---

## Web Portal Requests

Open the web portal at `http://192.168.1.14:5055`:

1. Search for the movie or TV show.
2. Select **Request**, choose target quality profile and seasons (for series), and submit.

---

## Status Definitions

| Status | Meaning |
| :--- | :--- |
| **Requested** | Request submitted and approved; waiting in queue. |
| **Processing** | Release located and downloading. |
| **Partially Available** | Subset of episodes completed and imported. |
| **Available** | Imported to media library and ready for streaming. |

---

## Quality Profile Guidelines

- **1080p (Standard):** Recommended default. Fast download times and balanced storage footprint (~2–9 GB per movie).
- **4K (2160p):** High-resolution profile for compatible displays. Requires roughly 3x the storage space and longer download duration (~20–40 GB per movie).

---

## Operational Notes

- Verify progress with `/status` before re-requesting media.
- Large releases and full TV seasons require extended download times depending on available sources.
- For stalled items, note the title and requested quality profile when contacting the system administrator.
