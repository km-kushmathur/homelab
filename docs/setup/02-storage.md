# 2 — Storage Configuration

**Objective:** Format the media storage disk with ext4, mount at `/mnt/media` using UUID and `nofail`, establish directory hierarchy, and set host UID permissions.

---

## Architectural Constraints

- **Single filesystem:** All media directories (`downloads`, `movies`, `tv`) must reside on the same filesystem to allow Linux hardlinks. Hardlinks cannot cross filesystem boundaries; crossing boundaries forces full file copies.
- **`nofail` mount option:** Required in `/etc/fstab` so the host boots cleanly if external storage is disconnected or slow to enumerate. Without `nofail`, an absent disk causes systemd to halt the boot sequence in emergency mode.

---

## Identify Target Disk

Connect storage to host and list block devices:

```bash
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,MODEL
```

```text
NAME   SIZE TYPE MOUNTPOINT MODEL
sda    477G disk            Samsung SSD 860
├─sda1   1G part /boot/efi
└─sda2  476G part
sdb    932G disk            Expansion
└─sdb1  932G part
```

Confirm target disk identifier by model and capacity before formatting.

---

## Partition and Format

> **Warning:** Destructive operation. All existing partitions and data on the target device will be erased.

```bash
DISK=/dev/sdb          # replace with target device identifier

wipefs -a "$DISK"
parted -s "$DISK" mklabel gpt
parted -s "$DISK" mkpart primary ext4 0% 100%
mkfs.ext4 -L media "${DISK}1"
```

Use `ext4` to ensure native POSIX ownership and hardlink support.

---

## Persistent Mount Configuration

Mount by UUID to prevent device identifier shifting across reboots (`/dev/sdb` vs `/dev/sdc`):

```bash
mkdir -p /mnt/media
blkid "${DISK}1"
```

```text
/dev/sdb1: LABEL="media" UUID="a1b2c3d4-..." TYPE="ext4" ...
```

Append configuration to `/etc/fstab`:

```bash
UUID=$(blkid -s UUID -o value "${DISK}1")
echo "UUID=$UUID  /mnt/media  ext4  defaults,nofail  0  0" >> /etc/fstab

mount -a
mountpoint -q /mnt/media && echo mounted
```

### Mount Options Reference

| Option | Function |
| :--- | :--- |
| `defaults` | Standard read-write mount settings (`rw, suid, dev, exec, auto, nouser, async`) |
| `nofail` | Prevents boot failure if the disk is absent or slow to enumerate |
| `0 0` | Disables dump backups and boot-time filesystem check (`fsck`) |

Reference template: [`config/examples/pve/host-fstab.example`](../../config/examples/pve/host-fstab.example).

> **Verification:** Test `nofail` behavior by rebooting without the external disk attached. The host must boot normally with `/mnt/media` empty. If boot halts at an emergency prompt, verify `/etc/fstab` syntax.

---

## Directory Structure and Ownership

Create base directory layout:

```bash
mkdir -p /mnt/media/{downloads,movies,tv,backups}
```

```text
/mnt/media/
├── downloads/     # in-progress and completed downloads
├── movies/        # imported films
├── tv/            # imported TV series
└── backups/       # application state archives
```

All directories reside on a single filesystem to support hardlink imports.

Set permissions for unprivileged LXC container mapping:

```bash
chown -R 101000:101000 /mnt/media
ls -la /mnt/media
```

```text
drwxr-xr-x 6 101000 101000 4096 ... .
drwxr-xr-x 2 101000 101000 4096 ... downloads
...
```

Unprivileged LXC containers map UID `1000` to host UID `101000` (via `+100000` offset). Setting host ownership to `101000` grants write permissions to container service processes running as UID `1000`.

> **Important:** Do not set ownership to host UID `1000` or execute `chmod 777`.

---

## Record the storage identity

Clone the repository to `/opt/homelab`. Run the following on the Proxmox host using the UUID of the filesystem you selected above. It must match the UUID in `/etc/fstab`.

```bash
install -d -m 0750 /etc/homelab
findmnt -n -M /mnt/media -o TARGET,UUID,FSTYPE,OPTIONS
```

Copy the [storage identity example](../../config/examples/pve/media-storage.json.example) to `/etc/homelab/media-storage.json` and replace the example UUID. After verifying the mounted device and UUID, write the same UUID to the marker on that filesystem.

```bash
mountpoint -q /mnt/media && [ "$(findmnt -n -M /mnt/media -o UUID)" = "$UUID" ] && \
  printf '%s\n' "$UUID" > /mnt/media/.homelab-media-identity
timeout 20 python3 /opt/homelab/scripts/media_storage.py --json
```

## Verification

```bash
mountpoint -q /mnt/media && echo "mounted"
df -h /mnt/media                  # verify mounted capacity
grep media /etc/fstab             # verify nofail option
ls -la /mnt/media                 # verify directories owned by 101000:101000
findmnt /mnt/media                # verify ext4, rw

# Verify hardlink capability across directory tree
touch /mnt/media/downloads/t && ln /mnt/media/downloads/t /mnt/media/movies/t \
  && echo "hardlinks OK" && rm -f /mnt/media/{downloads,movies}/t
```

Reboot and confirm automatic mount:

```bash
reboot
# after boot completes
mountpoint -q /mnt/media && echo "survived reboot"
```

---

## Troubleshooting

**Mount does not persist after reboot:** Check `journalctl -b | grep -i mnt-media`. Verify UUID match via `blkid`.

**Host halts in emergency mode when disk is disconnected:** Missing or invalid `nofail` option in `/etc/fstab`.

**`mkfs` reports device is busy:** Unmount active automounts (`umount /dev/sdb1`) and retry.

**Ownership resets to root after reboot:** Indicates mount failure; the host root directory underneath is being displayed. Verify with `mountpoint -q /mnt/media`.

---

**Next:** [3 — Containers](03-lxc-containers.md)
