# Python SMS Engine — API Reference

**Base URL:** `http://<server-ip>:9000`

---

## Authentication

All endpoints except `/health` require a shared secret header.

| Header | Value |
|---|---|
| `X-Gateway-Token` | Value of `SMS_PYTHON_API_TOKEN` env var on the Python server |

When `SMS_PYTHON_API_TOKEN` is unset or empty, auth is disabled (safe for local development).

**Unauthorized response (401):**
```json
{
    "success": false,
    "error": "UNAUTHORIZED"
}
```

---

## Endpoints Overview

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | No | Service liveness check |
| `POST` | `/send` | Yes | Send an SMS via a specific SIM |
| `GET` | `/modems/discover` | Yes | Force full hardware rescan |
| `GET` | `/modems/available` | Yes | List SMS-ready modems only |
| `GET` | `/modems/health` | Yes | Per-modem health status (cached) |
| `GET` | `/modems/summary` | Yes | Count of online/offline modems |
| `GET` | `/modems/debug` | Yes | Full raw modem state dump |
| `POST` | `/dev/stub/send-network-fail` | Yes | DEV ONLY — simulated network failure |

---

## GET /health

Liveness check. No authentication required. Use for uptime monitoring or load balancer probes.

**Request:** No body, no headers required.

**Response:**
```json
{
    "success": true,
    "service": "python_sms_engine",
    "status": "ok"
}
```

**curl example:**
```bash
curl http://localhost:9000/health
```

---

## POST /send

Send an SMS message through a specific SIM card, identified by its IMSI (`sim_id`).

Python handles port resolution, AT command sequence, retry on primary port, and fallback to secondary port. The caller only needs to provide the SIM identity and message.

**Request headers:**
```
X-Gateway-Token: <your-token>
Content-Type: application/json
```

**Request body:**
```json
{
    "sim_id": "515039219149367",
    "phone": "+639550090156",
    "message": "Hello world",
    "meta": {
        "message_id": 9001,
        "company_id": 42,
        "message_type": "CHAT"
    }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `sim_id` | string | Yes | IMSI of the SIM to send through |
| `phone` | string | Yes | Destination phone number (E.164 format recommended) |
| `message` | string | Yes | SMS body text |
| `meta` | object | No | Arbitrary caller context — echoed back unchanged, Python ignores it |

The `meta.message_id` field is extracted and echoed back in the top-level `message_id` field of the response so callers can correlate the response to their own record without parsing `raw`.

**Success response (200):**
```json
{
    "success": true,
    "message_id": "9001",
    "error": null,
    "raw": {
        "sim_id": "515039219149367",
        "modem_id": "866358071697796",
        "port": "/dev/ttyUSB2",
        "status": "success",
        "modem_response": "OK\r\n\nATE0\r\r\nOK\r\n\n\r\nOK\r\n\n\r\n> \n+CMGS: 118\r\n\r\nOK",
        "meta": {
            "message_id": 9001,
            "company_id": 42,
            "message_type": "CHAT"
        }
    }
}
```

`raw.status` can be:
- `"success"` — primary port succeeded
- `"retry_success"` — primary port failed, retry on same port succeeded
- `"fallback_success"` — both primary attempts failed, fallback port (if03) succeeded

**Failure response (200):**
```json
{
    "success": false,
    "message_id": "9001",
    "error": "SEND_FAILED",
    "raw": {
        "sim_id": "515039219149367",
        "modem_id": "866358071697796",
        "port": "/dev/ttyUSB2",
        "error_layer": "network",
        "cms_error_code": 50,
        "cme_error_code": null,
        "modem_response": "+CMS ERROR: 50",
        "meta": {
            "message_id": 9001,
            "company_id": 42,
            "message_type": "CHAT"
        }
    }
}
```

> Note: Failures still return HTTP 200. Check `success` in the body, not the HTTP status code.

**Response fields:**

| Field | Description |
|---|---|
| `success` | `true` = SMS accepted by carrier. `false` = failed at any layer. |
| `message_id` | Echo of `meta.message_id` as string, or `null` if not provided |
| `error` | Error code string (see error codes table below), or `null` on success |
| `raw.error_layer` | `"hardware"` / `"modem"` / `"network"` / `"unknown"` |
| `raw.cms_error_code` | Numeric carrier rejection code, or `null` |
| `raw.cme_error_code` | Numeric modem/equipment error code, or `null` |
| `raw.modem_id` | IMEI of the modem that was used, or `null` if not reached |
| `raw.port` | Serial port attempted (e.g. `/dev/ttyUSB2`), or `null` if not reached |
| `raw.meta` | Echo of the `meta` object sent in the request |

**curl example:**
```bash
curl -X POST http://localhost:9000/send \
  -H "X-Gateway-Token: your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "sim_id": "515039219149367",
    "phone": "+639550090156",
    "message": "Hello world",
    "meta": {"message_id": 1}
  }'
```

---

## Error Codes

### `error` field values

| Code | Meaning | `error_layer` | Retryable? |
|---|---|---|---|
| `SIM_NOT_MAPPED` | No modem found for this sim_id | `hardware` | Only after `/modems/discover` |
| `PORT_NOT_FOUND` | Serial port file gone from filesystem | `hardware` | Only after hardware fix |
| `MODEM_OPEN_FAILED` | Port busy or permission denied | `hardware` | Maybe, after delay |
| `MODEM_TIMEOUT` | No serial response within timeout | `hardware` | Yes, with backoff |
| `AT_NOT_RESPONDING` | Modem did not respond to AT command | `hardware` | Yes, with backoff |
| `CMGF_FAILED` | Failed to set SMS text mode | `hardware` | Yes |
| `CMGS_PROMPT_FAILED` | Modem rejected AT+CMGS (no `>` prompt) | `hardware` | Yes |
| `SEND_FAILED` | Message rejected by carrier — check `cms_error_code` | `network` | Depends on CMS code |
| `UNKNOWN_ERROR` | Unexpected exception | `unknown` | Retry once |

### `error_layer` — what to do per layer

| Layer | Meaning | Recommended action |
|---|---|---|
| `"hardware"` | Port dead, timeout, modem unplugged | Mark modem offline, reassign SIM |
| `"modem"` | SIM not inserted, PIN required, SIM failure | Mark SIM as errored, alert operator |
| `"network"` | No credit, invalid number, carrier blocked | Do NOT retry — log and return to sender |
| `"unknown"` | Unclassified | Retry once with backoff |

### Common `cms_error_code` values

| Code | Meaning |
|---|---|
| 27 | Destination unreachable |
| 38 | Network out of order — retry |
| 50 | Insufficient credit / no load |
| 350 | Invalid destination address — do not retry |

### Common `cme_error_code` values

| Code | Meaning |
|---|---|
| 10 | SIM not inserted |
| 11 | SIM PIN required |
| 13 | SIM failure |
| 14 | SIM busy — retry after short delay |

---

## GET /modems/discover

Forces a full hardware rescan by probing all USB ports via sysfs. Takes 2–5 seconds.

**When to use:** Modem just plugged in, after server restart, or to refresh the full modem inventory. Do not call this on every send — the registry warm cache handles routine lookups.

**Request headers:**
```
X-Gateway-Token: <your-token>
```

**Response:**
```json
{
    "success": true,
    "modems": [
        {
            "sim_id": "515039219149367",
            "modem_id": "866358071697796",
            "device_id": "3-7.4.4",
            "port": "/dev/ttyUSB2",
            "fallback_port": "/dev/ttyUSB3",
            "interface": "if02",
            "at_ok": true,
            "sim_ready": true,
            "creg_registered": true,
            "signal": "+CSQ: 20,99",
            "imsi": "515039219149367",
            "iccid": "89630323255005160625",
            "imei": "866358071697796"
        }
    ]
}
```

| Field | Description |
|---|---|
| `sim_id` | IMSI — use this as the key for `/send` |
| `modem_id` | IMEI — hardware identity, use for inventory tracking |
| `iccid` | Physical SIM card serial number |
| `device_id` | USB physical address (sysfs) — stable across reboots |
| `port` | Primary serial port (interface if02) |
| `fallback_port` | Fallback serial port (interface if03) |
| `at_ok` | `true` = modem responds to AT commands |
| `sim_ready` | `true` = SIM is inserted and readable |
| `creg_registered` | `true` = registered on carrier network |
| `signal` | Signal strength string from `AT+CSQ` |

**curl example:**
```bash
curl http://localhost:9000/modems/discover \
  -H "X-Gateway-Token: your-token"
```

---

## GET /modems/available

Returns only modems that are fully SMS-ready: `at_ok=true`, `sim_ready=true`, and `creg_registered=true`.

Use before dispatching a batch of messages to check how many modems are usable.

**Request headers:**
```
X-Gateway-Token: <your-token>
```

**Response:** Same shape as `/modems/discover` — only modems that pass all three readiness checks are included.

```json
{
    "success": true,
    "modems": [
        {
            "sim_id": "515039219149367",
            "modem_id": "866358071697796",
            "port": "/dev/ttyUSB2",
            "at_ok": true,
            "sim_ready": true,
            "creg_registered": true,
            ...
        }
    ]
}
```

If no modems are ready, `modems` is an empty array:
```json
{
    "success": true,
    "modems": []
}
```

**curl example:**
```bash
curl http://localhost:9000/modems/available \
  -H "X-Gateway-Token: your-token"
```

---

## GET /modems/health

Lightweight per-modem health check. Uses cached registry state — no serial I/O performed.

**When to use:** Frequent polling (e.g. every 60 seconds) to detect modems going offline without triggering a full rescan.

**Request headers:**
```
X-Gateway-Token: <your-token>
```

**Response:**
```json
{
    "success": true,
    "modems": [
        {
            "sim_id": "515039219149367",
            "modem_id": "866358071697796",
            "port": "/dev/ttyUSB2",
            "reachable": true,
            "at_ok": true
        }
    ]
}
```

| Field | Description |
|---|---|
| `sim_id` | IMSI of the SIM in this modem |
| `modem_id` | IMEI — use to correlate to your hardware inventory |
| `port` | Serial port currently mapped to this modem |
| `reachable` | `true` = port file exists on filesystem |
| `at_ok` | `true` = modem responded to AT at last check |

**curl example:**
```bash
curl http://localhost:9000/modems/health \
  -H "X-Gateway-Token: your-token"
```

---

## GET /modems/summary

Quick count of modem states. Intended for dashboards and monitoring.

**Request headers:**
```
X-Gateway-Token: <your-token>
```

**Response:**
```json
{
    "success": true,
    "summary": {
        "total": 5,
        "online": 4,
        "offline": 1
    }
}
```

| Field | Description |
|---|---|
| `total` | Total modems detected in registry |
| `online` | Modems with `at_ok=true` |
| `offline` | Modems with `at_ok=false` |

**curl example:**
```bash
curl http://localhost:9000/modems/summary \
  -H "X-Gateway-Token: your-token"
```

---

## GET /modems/debug

Full raw dump of the internal modem registry state. Returns every field stored per modem — useful for debugging discovery, port mapping, or signal issues.

**When to use:** Debugging only. Not intended for production polling.

**Request headers:**
```
X-Gateway-Token: <your-token>
```

**Response:**
```json
{
    "success": true,
    "modems": [
        {
            "sim_id": "515039219149367",
            "modem_id": "866358071697796",
            "port": "/dev/ttyUSB2",
            "fallback_port": "/dev/ttyUSB3",
            "device_id": "3-7.4.4",
            "interface": "if02",
            "at_ok": true,
            "sim_ready": true,
            "creg_registered": true,
            "signal": "+CSQ: 20,99",
            "imsi": "515039219149367",
            "iccid": "89630323255005160625",
            "imei": "866358071697796"
        }
    ]
}
```

**curl example:**
```bash
curl http://localhost:9000/modems/debug \
  -H "X-Gateway-Token: your-token"
```

---

## POST /dev/stub/send-network-fail

**DEV ONLY — Remove before production deployment.**

Returns a deterministic terminal network-layer failure response without touching any modem hardware. Used to prove that callers correctly handle the `error_layer=network` fast-fail path.

Accepts the same request body as `/send`. Returns a hardcoded `+CMS ERROR: 27` (destination unreachable) response.

**Request body:** Same as `/send`.

**Response:**
```json
{
    "success": false,
    "message_id": "9001",
    "error": "SEND_FAILED",
    "raw": {
        "sim_id": "515039219149367",
        "modem_id": "STUB_MODEM_ID",
        "port": "/dev/ttyUSB_STUB",
        "error_layer": "network",
        "cms_error_code": 27,
        "cme_error_code": null,
        "modem_response": "+CMS ERROR: 27",
        "meta": {
            "message_id": 9001
        }
    }
}
```

**curl example:**
```bash
curl -X POST http://localhost:9000/dev/stub/send-network-fail \
  -H "X-Gateway-Token: your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "sim_id": "any",
    "phone": "+639550090156",
    "message": "test",
    "meta": {"message_id": 9001}
  }'
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SMS_ENGINE_PORT` | `8000` | HTTP server port (production uses `9000`) |
| `SMS_PYTHON_API_TOKEN` | `` | Shared secret for `X-Gateway-Token`. Auth disabled when empty. |
| `SMS_ENGINE_SERIAL_TIMEOUT` | `3` | Serial read timeout in seconds |
| `SMS_ENGINE_COMMAND_TIMEOUT` | `10` | Per-AT-command timeout in seconds |
| `SMS_ENGINE_SEND_TIMEOUT` | `30` | Full send sequence timeout in seconds |

Set HTTP client timeout to at least **35 seconds** on the caller side — `SMS_ENGINE_SEND_TIMEOUT` defaults to 30s, and your client must be higher to avoid false timeouts.

---

## Quick Reference

```bash
# Health check (no auth)
curl http://localhost:9000/health

# Send SMS
curl -X POST http://localhost:9000/send \
  -H "X-Gateway-Token: secret" \
  -H "Content-Type: application/json" \
  -d '{"sim_id":"515039219149367","phone":"+63912345678","message":"Hello","meta":{"message_id":1}}'

# Discover modems
curl http://localhost:9000/modems/discover -H "X-Gateway-Token: secret"

# Available modems
curl http://localhost:9000/modems/available -H "X-Gateway-Token: secret"

# Health per modem
curl http://localhost:9000/modems/health -H "X-Gateway-Token: secret"

# Summary count
curl http://localhost:9000/modems/summary -H "X-Gateway-Token: secret"

# Debug dump
curl http://localhost:9000/modems/debug -H "X-Gateway-Token: secret"
```
