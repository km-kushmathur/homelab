# 1 — Proxmox Host Setup

**Objective:** Install Proxmox VE hypervisor, assign a static LAN address, configure package repositories, and apply base security settings.

> Note: IP addresses in this guide are illustrative. `192.168.1.10` represents the hypervisor host, `192.168.1.1` the default gateway, and `.11+` the containers. Substitute local subnet values as appropriate.

---

## Prerequisites

- x86-64 machine with virtualization extensions (Intel VT-x / AMD-V), 4+ threads, and 16 GB RAM.
- Wired Ethernet connection.
- USB flash drive (1 GB+).
- Display and keyboard for initial installation.
- If using a laptop as the host hardware, configure lid-close behavior to prevent system suspension (detailed below).

> **Warning:** The Proxmox installer erases the target disk. Back up necessary data before proceeding.

---

## Installation

**1. Flash installation media:** Download Proxmox VE ISO from [proxmox.com/downloads](https://www.proxmox.com/en/downloads) and write to USB:

```bash
# Verify target block device before running
sudo dd if=proxmox-ve_9.x.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

**2. Boot:** Disable Secure Boot in BIOS/UEFI if prompted. Select **Install Proxmox VE (Graphical)**.

**3. Target disk:** Select target SSD (internal drive for hypervisor and container root filesystems, not the external media disk). Default filesystem (`ext4`) is recommended.

**4. Credentials:** Set the root password and administrative email address.

**5. Network configuration:**

| Field | Value | Notes |
| :--- | :--- | :--- |
| Management interface | Primary wired NIC | Exclude Wi-Fi adapters |
| Hostname (FQDN) | `pve.local` | Node hostname |
| IP address (CIDR) | `192.168.1.10/24` | Static address outside DHCP pool |
| Gateway | `192.168.1.1` | LAN default gateway |
| DNS server | `192.168.1.1` or upstream DNS | Resolver address |

Complete the installation and reboot. The console will display the management URL:

```text
https://192.168.1.10:8006/
```

---

## Package Repository Configuration

Navigate to `https://192.168.1.10:8006/` in a browser. Accept the self-signed TLS certificate and log in as `root` using **Linux PAM standard authentication**.

The default installation enables the enterprise repository, which returns HTTP 401 without a subscription. Configure the `pve-no-subscription` repository from the host shell:

```bash
# Disable enterprise repositories
sed -i 's/^deb/#deb/' /etc/apt/sources.list.d/pve-enterprise.sources 2>/dev/null || true
sed -i 's/^deb/#deb/' /etc/apt/sources.list.d/ceph.sources 2>/dev/null || true

# Add no-subscription repository
cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

apt update && apt full-upgrade -y
```

Reboot if a new kernel was installed.

**Verification:**

```bash
apt update        # completes with 0 errors
pveversion        # displays pve-manager/9.x
```

---

## Base Hardening

**Key-based SSH:** From the client workstation:

```bash
ssh-copy-id root@192.168.1.10
ssh root@192.168.1.10        # verify passwordless authentication
```

Disable password authentication:

```bash
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

> **Warning:** Verify SSH key login in an independent terminal session before terminating the current session.

**Automatic security updates:**

```bash
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

**Timezone and NTP:**

```bash
timedatectl set-timezone <Region/City>
timedatectl                       # verify NTP synchronization is active
```

---

## Laptop Lid-Close Configuration

Prevent system suspension on lid-close when running on laptop hardware:

```bash
sed -i 's/^#*HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sed -i 's/^#*HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' /etc/systemd/logind.conf
systemctl restart systemd-logind
```

---

## Verification

```bash
pveversion                    # pve-manager/9.x
ip -4 addr show vmbr0         # verify 192.168.1.10/24
ping -c2 192.168.1.1          # gateway connectivity
ping -c2 1.1.1.1              # external network connectivity
apt update                    # exit status 0
free -h; lscpu | head -20     # verify available memory and CPU threads
```

`vmbr0` provides bridged networking for container assignments on the local LAN subnet.

---

## Troubleshooting

**Web UI unreachable:** Check network address via local console (`ip -4 addr`). Ensure installer attached to the wired NIC rather than wireless interface.

**`apt update` returns HTTP 401:** Enterprise repository remains active. Verify files in `/etc/apt/sources.list.d/`.

**Installer fails to detect disk:** Verify SATA/NVMe controller is configured for AHCI rather than RAID in BIOS/UEFI.

**Virtualization extensions unavailable:** Enable VT-x or AMD-V in firmware.

---

**Next:** [2 — Storage](02-storage.md)
