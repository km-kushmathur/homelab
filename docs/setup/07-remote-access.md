# 7 — Remote Access

Configure secure remote access via Tailscale subnet routing and ACLs without exposing ports publicly.

---

## Network Model

Tailscale creates an encrypted overlay network using WireGuard. Connecting via Tailscale avoids port forwarding and leaves no listening services exposed on the public WAN.

Access is partitioned by role:
- **Administrators:** Full management access across all subnets and hypervisor interfaces.
- **Members:** Access restricted strictly to the media server (`8096`) and request portal (`5055`).

---

## Host Installation

Install Tailscale on the hypervisor host and advertise the local subnet:

```bash
curl -fsSL https://tailscale.com/install.sh | sh

tailscale up --advertise-tags=tag:homelab --advertise-routes=192.168.1.0/24
```

Authenticate via the provided URL.

| Parameter | Purpose |
| :--- | :--- |
| `--advertise-tags=tag:homelab` | Tags the node for role-based ACL enforcement |
| `--advertise-routes=192.168.1.0/24` | Advertises local subnet routes for container access |

Approve the subnet route in the Tailscale admin console (**Machines → Host → Route settings**).

Enable IPv4 and IPv6 packet forwarding on the host:

```bash
echo 'net.ipv4.ip_forward = 1' >> /etc/sysctl.d/99-tailscale.conf
echo 'net.ipv6.conf.all.forwarding = 1' >> /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf
```

Verify host Tailscale IP:

```bash
tailscale ip -4
```

---

## Access Control Policy

Configure ACLs to restrict member devices from accessing hypervisor and administrative ports.

In the Tailscale admin console (**Access Controls**), apply the rules from [`config/templates/tailscale/acl.json`](../../config/templates/tailscale/acl.json):

```json
"groups": {
    "group:admin":  ["your-email@example.com"],
    "group:member": ["their-email@example.com"],
},
```

Define destination boundaries:

```json
"dst": [
    "192.168.1.11:8096",
    "192.168.1.14:5055",
],
```

> **Security Requirement:** Do not grant `group:member` unrestricted destination access (`"*:*"`). Keep member access scoped strictly to ports `8096` and `5055`.

---

## Member Device Enrollment

Invite members via **Settings → Users → Invite external users** using the email addresses assigned to `group:member`. Users install the Tailscale client and authenticate.

Enrolled devices can access the media server at `http://192.168.1.11:8096` and the request portal at `http://192.168.1.14:5055`.

---

## Verification

From the host:

```bash
tailscale status
tailscale ip -4
```

From an **admin** device on the tailnet:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.11:8096/health   # 200
curl -sk -o /dev/null -w '%{http_code}\n' https://192.168.1.10:8006        # 200
```

From a **member** device, verify access boundaries:

```bash
curl -s  -o /dev/null -w '%{http_code}\n' http://192.168.1.11:8096/health  # 200 (allowed)
curl -s  -o /dev/null -w '%{http_code}\n' http://192.168.1.14:5055         # 200 (allowed)
curl -sk -o /dev/null -w '%{http_code}\n' --max-time 5 https://192.168.1.10:8006  # connection timed out (blocked)
curl -s  -o /dev/null -w '%{http_code}\n' --max-time 5 http://192.168.1.14:8080   # connection timed out (blocked)
```

Confirm external access fails when Tailscale is disabled (verifying ports are not exposed to public WAN):

```bash
curl --max-time 5 http://<your-public-ip>:8096     # connection timed out (blocked)
```

---

## Troubleshooting

**Subnet unreachable:** Confirm route is approved under **Machines → Host → Route settings** in the admin console.

**Route approved but packets drop:** IP forwarding is inactive. Check `sysctl net.ipv4.ip_forward` and apply `/etc/sysctl.d/99-tailscale.conf`.

**High latency / relaying:** Run `tailscale status`. If connections report `relay`, direct peer connection could not be negotiated due to NAT constraints.

**Service not persisting across reboots:** Verify the service is enabled: `systemctl is-enabled tailscaled`.

---

**Next:** [8 — Notifications](08-notifications.md)
