# 8 — Notifications

Configure push notification channels using ntfy for media lifecycle and error alerts.

---

## Architecture

ntfy provides lightweight HTTP pub/sub push notifications without account registration or app store requirements. Alerts are published by HTTP POST requests to a designated topic.

---

## Topic Selection

Generate a high-entropy topic identifier:

```bash
openssl rand -hex 12
```

Use the generated hex string as part of the topic, e.g. `homelab-7f3a9c2e8b1d`.

> **Security Requirement:** Treat the topic string as a secret credential. Public topics do not enforce authentication; anyone with the topic name can read or publish alerts. Store this value in `/etc/homelab/homelab.env`.

Install the ntfy client application and subscribe to the topic.

Validate the delivery endpoint:

```bash
curl -d "test notification" https://ntfy.sh/<your-topic>
```

---

## Service Integration

### Request portal

**Settings → Notifications → ntfy.sh**:

| Field | Value |
| :--- | :--- |
| Server URL | `https://ntfy.sh` |
| Topic | Configured topic string |

Enable **Request Automatically Approved**. Enable **Request Pending Approval** only if auto-approval is disabled to avoid duplicate alerts.

### Radarr and Sonarr

**Settings → Connect → + → ntfy** (in both applications):

| Field | Value |
| :--- | :--- |
| Trigger | On Grab |
| Server URL | `https://ntfy.sh` |
| Topic | Configured topic string |
| Click Action URL | `http://192.168.1.14:8080` |

The click action opens the download client web UI to view active transfer metrics.

Validate connection using the **Test** button in each application.

---

## Self-Hosting (Optional)

The compose stack includes an ntfy instance at `http://192.168.1.14`. Configure applications to target `http://192.168.1.14` instead of `https://ntfy.sh`.

> **Configuration Requirement:** `NTFY_BASE_URL` and `NTFY_UPSTREAM_BASE_URL` must be set simultaneously in `.env` to prevent validation errors.

---

## Environment Configuration

Define the notification endpoints for use in `/etc/homelab/homelab.env`:

```bash
NTFY_BASE_URL=https://ntfy.sh
NTFY_TOPIC=homelab-7f3a9c2e8b1d
NTFY_STREAM_URL=https://ntfy.sh/homelab-7f3a9c2e8b1d/json
```

`NTFY_STREAM_URL` provides the JSON event stream subscribed to by the Discord bridge ([chapter 10](10-discord-agent.md)).

---

## Verification

Send a manual test payload:

```bash
curl -d "manual test payload" https://ntfy.sh/<your-topic>
```

Submit a test media request in the portal to verify automated event delivery.

---

## Troubleshooting

**Notifications not received:** Verify topic string exact matching (case-sensitive) and check client notification permissions.

**Self-hosted container fails startup:** Verify both `NTFY_BASE_URL` and `NTFY_UPSTREAM_BASE_URL` are defined in `.env`.

**Events not forwarding to Discord:** Confirm `NTFY_STREAM_URL` points to the `/json` stream endpoint. Check bridge logs: `journalctl -u homelab-bot -n 50`.

---

**Next:** [9 — Storage watchdog](09-storage-watchdog.md)
