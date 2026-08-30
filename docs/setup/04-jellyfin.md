# 4 — Media Server

Deploy Jellyfin in container 201 with shared storage mounts and hardware transcoding.

---

## Install

Jellyfin runs natively in container 201 rather than inside Docker to simplify direct GPU device passthrough.

```bash
pct exec 201 -- bash -c '
apt install -y curl gnupg
curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg
echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian $(. /etc/os-release; echo $VERSION_CODENAME) main" \
  > /etc/apt/sources.list.d/jellyfin.list
apt update && apt install -y jellyfin
'
pct exec 201 -- systemctl enable --now jellyfin
pct exec 201 -- systemctl is-active jellyfin      # active
```

Reachable at `http://192.168.1.11:8096`.

---

## Hardware transcoding

Enable hardware acceleration to offload video transcoding from the host CPU to an integrated GPU (iGPU).

### Identify host device GIDs

Device GIDs differ between distributions. Query the local group IDs before configuring passthrough:

```bash
# On the hypervisor host
ls -la /dev/dri
```

```text
crw-rw---- 1 root video  226,   0 ... card0
crw-rw---- 1 root render 226, 128 ... renderD128
```

```bash
getent group render     # render:x:993:
getent group video      # video:x:44:
```

Record the GIDs and device names (e.g. `card0` or `card1`).

> **If `/dev/dri` does not exist**, the host has no usable GPU or the driver is not loaded. Install `intel-media-va-driver-non-free` on the host and reboot. Without `/dev/dri`, skip this section (software transcoding will be used).

### Pass devices through to container

```bash
pct set 201 -dev0 /dev/dri/renderD128,gid=993
pct set 201 -dev1 /dev/dri/card0,gid=44
pct stop 201 && pct start 201
```

Substitute local device names and GIDs.

### Install drivers in container

```bash
pct exec 201 -- apt install -y intel-media-va-driver-non-free vainfo
pct exec 201 -- vainfo
```

`vainfo` will list supported profiles including `VAProfileH264Main` and `VAProfileHEVCMain`.

### Enable hardware acceleration in Jellyfin

Navigate to **Dashboard → Playback → Transcoding**:

| Setting | Value |
| :--- | :--- |
| Hardware acceleration | Video Acceleration API (VAAPI) |
| VA-API device | `/dev/dri/renderD128` |
| Enable hardware decoding | H264, HEVC, VP9 (as listed by `vainfo`) |
| Hardware encoding | Enabled |
| Tone mapping | Enabled (if HDR content is present) |

Save configuration.

---

## Initial setup wizard

Open `http://192.168.1.11:8096`.

1. **Language:** Select default display language.
2. **Admin account:** Set administrator credentials.
3. **Media libraries:** Add the mounted media paths:

   | Library | Content type | Folder |
   | :--- | :--- | :--- |
   | Movies | Movies | `/mnt/media/movies` |
   | TV Shows | Shows | `/mnt/media/tv` |

   > **Note:** Specify the bind mount path directly (`/mnt/media/movies` and `/mnt/media/tv`).

4. **Metadata language:** Configure preferred language.
5. **Remote access:** Leave *"Allow remote connections"* enabled and leave UPnP port mapping disabled. Remote access is routed via Tailscale ([chapter 7](07-remote-access.md)).

---

## Create an API key

Create an API key for the watchdog and CLI automation:

Navigate to **Dashboard → Advanced → API Keys → +**, name it `homelab-automation`, and copy the key. Store it for use in `/etc/homelab/homelab.env` as `JELLYFIN_API_KEY` ([chapter 9](09-storage-watchdog.md)).

---

## Verification

```bash
pct exec 201 -- systemctl is-active jellyfin
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.11:8096/health    # 200
pct exec 201 -- ls -la /mnt/media/movies
pct exec 201 -- vainfo | head -20
```

Verify transcoding behavior:

1. Place a test video file into `/mnt/media/movies/`.
2. Ensure correct ownership: `chown 101000:101000 /mnt/media/movies/<file>`.
3. Trigger a library scan and play the file, selecting a lower bitrate in the client to trigger transcoding.
4. Check **Dashboard → Activity** to confirm the session reports `Transcoding (hw)`.

If the session reports `Transcoding (sw)`, verify GID mapping and `vainfo` output inside container 201.

---

## Troubleshooting

**Library scan finds no files.** Verify directory ownership (`101000:101000` on host, mapped to `1000:1000` inside container). Confirm Jellyfin read access:
`pct exec 201 -- sudo -u jellyfin ls /mnt/media/movies`.

**`vainfo` fails inside container.** Passthrough GIDs do not match host groups. Verify `getent group render` and `video` on the host and update container configuration with `pct set`.

**High CPU usage during playback.** Transcoding is running on CPU. Confirm hardware acceleration is enabled and the relevant codec is enabled in Jellyfin settings.

**Direct play functions but transcoding fails.** Check device node permissions on `/dev/dri` inside container 201: `pct exec 201 -- ls -la /dev/dri`. Confirm the device group matches the `jellyfin` service user group.

---

**Next:** [5 — Media stack](05-media-stack.md)

