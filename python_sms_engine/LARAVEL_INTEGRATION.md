# Laravel Integration Report — Python SMS Engine

This document is the authoritative integration contract between the Laravel SMS Gateway (control plane) and the Python SMS Engine (execution plane).

**For Laravel Claude:** Read this before writing any HTTP client, service class, or job that talks to Python.

---

## Architecture Boundary

| Layer | Owner | Responsibility |
|---|---|---|
| **Control plane** | Laravel | Queueing, retry policy, assignment, operator status, business logic, database |
| **Execution plane** | Python | Serial port I/O, AT commands, modem discovery, hardware execution |

Python does **not** know about queues, tenants, retry counts, or business rules.
Laravel does **not** know about ttyUSB ports, AT commands, or modem hardware.

---

## Base URL

```
http://<server-ip>:9000
```

Default port: `9000`. Configure via `SMS_ENGINE_PORT` env var on the Python side.

---

## Authentication

All Laravel-facing endpoints require a shared secret header. `/health` is intentionally unprotected.

| Item | Value |
|---|---|
| Header name | `X-Gateway-Token` |
| Laravel env key | `SMS_PYTHON_API_TOKEN` |
| Python env key | `SMS_PYTHON_API_TOKEN` |
| Unauthorized response | `401 {"success": false, "error": "UNAUTHORIZED"}` |
| Auth disabled when | `SMS_PYTHON_API_TOKEN` is unset or empty (local dev) |

Laravel must send this header on every protected request:
```php
Http::baseUrl(config('sms.python_engine_url'))
    ->timeout(35)
    ->withHeaders(['X-Gateway-Token' => config('sms.python_api_token')])
    ->post('/send', [...]);
```

---

## Endpoints

| Method | Path | Auth required | Purpose |
|---|---|---|---|
| `GET` | `/health` | No | Service liveness check |
| `POST` | `/send` | Yes | Send an SMS via a specific SIM |
| `GET` | `/modems/discover` | Yes | Force full rescan, return all discovered modems |
| `GET` | `/modems/available` | Yes | Return only SMS-ready modems |
| `GET` | `/modems/health` | Yes | Per-modem health status |
| `GET` | `/modems/summary` | Yes | Count of online/offline modems |
| `GET` | `/modems/debug` | Yes | Full raw modem state dump (debugging only) |

---

## `GET /health`

Liveness check. No authentication required. Use this for uptime monitoring.

**Response:**
```json
{
    "success": true,
    "service": "python_sms_engine",
    "status": "ok"
}
```

---

## `POST /send`

Send an SMS message through a specific SIM (identified by IMSI).

### Request

```json
{
    "sim_id": "515039219149367",
    "phone": "+639550090156",
    "message": "Hello from Laravel",
    "meta": {
        "message_id": 9001,
        "company_id": 42,
        "message_type": "CHAT"
    }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `sim_id` | string | yes | IMSI of the SIM to send through |
| `phone` | string | yes | Destination phone number |
| `message` | string | yes | SMS body text |
| `meta` | object | no | Arbitrary Laravel context — echoed back unchanged, Python ignores it |

**`meta` usage:** Laravel should include `message_id` (its internal queue/message ID) so it can correlate Python's response back to its own record without parsing `raw`.

### Success Response

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

| Field | Description |
|---|---|
| `success` | `true` = SMS accepted by carrier network |
| `message_id` | Echo of `meta.message_id` as string, or `null` if not provided |
| `raw.status` | `"success"` / `"retry_success"` / `"fallback_success"` |
| `raw.modem_id` | IMEI of the hardware modem that sent it |
| `raw.port` | Serial port used (`/dev/ttyUSBX`) |

### Failure Response

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

| Field | Description |
|---|---|
| `success` | `false` |
| `error` | Error code string (see table below) |
| `raw.error_layer` | `"hardware"` / `"modem"` / `"network"` / `"unknown"` |
| `raw.cms_error_code` | Numeric carrier error code, or `null` |
| `raw.cme_error_code` | Numeric modem/equipment error code, or `null` |
| `raw.modem_id` | IMEI if modem was reached, `null` if not |
| `raw.port` | Port attempted, or `null` if not reached |

---

## Error Codes

### `error` field values

| Code | Meaning | Retryable? |
|---|---|---|
| `SIM_NOT_MAPPED` | No modem found for this sim_id in registry | Only after rediscovery |
| `PORT_NOT_FOUND` | Serial port file gone from filesystem | Only after hardware fix |
| `MODEM_OPEN_FAILED` | Port busy or permission denied | Maybe, after delay |
| `MODEM_TIMEOUT` | No serial response within timeout | Yes, with backoff |
| `AT_NOT_RESPONDING` | Modem did not respond to AT command | Yes, with backoff |
| `CMGF_FAILED` | Failed to set SMS text mode | Yes |
| `CMGS_PROMPT_FAILED` | Modem rejected AT+CMGS (no `>` prompt) | Yes |
| `SEND_FAILED` | Message rejected — check `cms_error_code` | Depends on CMS code |
| `UNKNOWN_ERROR` | Unexpected exception | Maybe |

### `error_layer` — Laravel retry policy guide

| Layer | Value | Meaning | Laravel action |
|---|---|---|---|
| Hardware failure | `"hardware"` | Port dead, timeout, modem unplugged | Mark modem offline, reassign SIM to another modem |
| Modem/SIM failure | `"modem"` | SIM not inserted, PIN, SIM failure | Mark SIM as errored, notify operator |
| Carrier rejection | `"network"` | No credit, invalid number, carrier blocked | Do NOT retry — log and return to sender |
| Unknown | `"unknown"` | Unclassified | Retry once with backoff |

### Common `cms_error_code` values (network layer)

| Code | Meaning | Laravel action |
|---|---|---|
| 27 | Destination unreachable | Mark number as invalid |
| 38 | Network out of order | Retry with backoff |
| 50 | Insufficient credit / no load | Mark SIM as no-credit |
| 350 | Invalid destination address | Mark number as invalid, do not retry |

### Common `cme_error_code` values (modem layer)

| Code | Meaning | Laravel action |
|---|---|---|
| 10 | SIM not inserted | Mark modem as hardware error |
| 11 | SIM PIN required | Alert operator |
| 13 | SIM failure | Mark SIM as failed |
| 14 | SIM busy | Retry after short delay |

---

## `GET /modems/discover`

Forces a full hardware rescan. All modem probes run in parallel with a hard per-modem timeout (12s). The endpoint always returns within ~15s regardless of hardware state — it does not hang.

**Use when:** Modem was just plugged in, after server restart, or to refresh the full modem inventory.

**Do not call on every send request** — the registry has a warm cache that handles routine lookups.

**Partial results are expected behavior.** The response includes ALL detected ports, including unhealthy and timed-out ones. A response where some modems have `probe_error` set is not a failure — it means those specific devices need attention. Healthy modems in the same response are usable.

**Response (healthy modem):**
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
            "imei": "866358071697796",
            "probe_error": null,
            "send_ready": true,
            "identifier_source": "imsi"
        }
    ]
}
```

**Failed/timed-out modem (in same response as healthy ones):**
```json
{
    "sim_id": "3-7.2.4",
    "modem_id": null,
    "at_ok": false,
    "sim_ready": false,
    "creg_registered": false,
    "probe_error": "PROBE_TIMEOUT after 12.0s",
    "send_ready": false,
    "identifier_source": "fallback_device_id"
}
```

| Field | Description |
|---|---|
| `sim_id` | Primary SIM identifier used for routing. Source depends on `identifier_source`. |
| `modem_id` | IMEI — hardware identity, use for inventory tracking |
| `iccid` | SIM card serial number |
| `device_id` | USB physical address (sysfs) — stable across reboots |
| `signal` | Signal strength string from `AT+CSQ` |
| `creg_registered` | `true` = registered on carrier network |
| `probe_error` | `null` = healthy. Error string = probe failed or timed out. Inspect per device. |
| **`send_ready`** | **`true` = this row is safe to use as a `/send` target. Use this directly.** |
| **`identifier_source`** | **`"imsi"` = `sim_id` is a real telecom IMSI. `"fallback_device_id"` = IMSI unavailable, not a valid SIM identity for routing.** |

**`send_ready` rule:** `true` only when all hold: `probe_error=null`, `at_ok=true`, `sim_ready=true`, `creg_registered=true`, and `identifier_source="imsi"`. If any condition fails, `send_ready=false`.

**Laravel handling of partial results:**

```php
$response = Http::baseUrl($engineUrl)
    ->withHeaders(['X-Gateway-Token' => config('sms.python_api_token')])
    ->get('/modems/discover');

$modems = $response->json()['modems'];

foreach ($modems as $modem) {
    if (! $modem['send_ready']) {
        // Row is not usable for sending — log and skip
        // probe_error and identifier_source tell you why
        Log::warning('Modem not send-ready', [
            'device_id'         => $modem['device_id'],
            'port'              => $modem['port'],
            'probe_error'       => $modem['probe_error'],
            'identifier_source' => $modem['identifier_source'],
        ]);
        continue;
    }

    // send_ready=true guarantees:
    //   - probe_error is null
    //   - at_ok, sim_ready, creg_registered are all true
    //   - sim_id is a real IMSI (identifier_source="imsi")
    // Safe to use $modem['sim_id'] as the key for /send
}
```

---

## `GET /modems/available`

Returns only modems that are SMS-ready (`at_ok=true`, `sim_ready=true`, `creg_registered=true`).

Use this to check how many modems are usable before dispatching a batch.

Same response shape as `/modems/discover`.

---

## `GET /modems/health`

Lightweight health check per modem. Uses cached registry state — no serial I/O.

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

Use `modem_id` (IMEI) to correlate health items to your hardware inventory table.

---

## `GET /modems/summary`

Quick count for dashboards and monitoring.

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

---

## Laravel Integration Checklist

### HTTP Client setup

```php
// Recommended: dedicated service class
// Base URL from config, timeout slightly above Python's send_timeout (default 30s)
Http::baseUrl(config('sms.python_engine_url'))
    ->timeout(35)
    ->withHeaders(['X-Gateway-Token' => config('sms.python_api_token')])
    ->post('/send', [...]);
```

### Sending an SMS

```php
$response = Http::baseUrl($engineUrl)
    ->timeout(35)
    ->post('/send', [
        'sim_id'  => $sim->imsi,
        'phone'   => $message->phone,
        'message' => $message->body,
        'meta'    => [
            'message_id'   => $message->id,
            'company_id'   => $message->company_id,
            'message_type' => $message->type,
        ],
    ]);

$data = $response->json();

if ($data['success']) {
    // SMS delivered — mark message as sent
    // $data['message_id'] == (string) $message->id
} else {
    $layer = $data['raw']['error_layer'];    // hardware / modem / network / unknown
    $cms   = $data['raw']['cms_error_code']; // nullable int
    $cme   = $data['raw']['cme_error_code']; // nullable int

    match ($layer) {
        'network'  => // do NOT retry, log cms code
        'modem'    => // mark SIM as errored, alert operator
        'hardware' => // mark modem offline, reassign SIM
        default    => // retry once with backoff
    };
}
```

### Polling modem availability

```php
// Before dispatching a batch job, check if any modems are ready
$response = Http::baseUrl($engineUrl)->get('/modems/available');
$available = $response->json()['modems'];

if (empty($available)) {
    // No modems ready — delay dispatch or alert
}

// Build sim_id → modem mapping for assignment
foreach ($available as $modem) {
    // $modem['sim_id']   = IMSI (use as /send key)
    // $modem['modem_id'] = IMEI (use for inventory)
    // $modem['iccid']    = SIM card number
}
```

### Periodic health monitoring

```php
// Suggested: run every 60 seconds via scheduled command
$response = Http::baseUrl($engineUrl)->get('/modems/summary');
$summary = $response->json()['summary'];

// $summary['online']  = usable modem count
// $summary['offline'] = degraded modem count
// $summary['total']   = total detected modem count
```

---

## Key Rules for Laravel

1. **Route by `sim_id` (IMSI)** — always send the IMSI string, never a port or ttyUSB number
2. **Always include `meta.message_id`** — lets you correlate response to your DB record via `response['message_id']`
3. **Always send `X-Gateway-Token`** — all protected endpoints reject without it
4. **Check `error_layer` before deciding to retry** — do not blindly retry `network` errors
5. **Do not call `/modems/discover` on every send** — it triggers a full parallel hardware scan; use it only for inventory sync or after hardware changes
6. **Use `/modems/available` before batch dispatch** — not on individual sends (registry warm cache handles that)
7. **Set HTTP timeout to 35s** — Python's default `send_timeout` is 30s; your client must be higher or you'll get false timeouts
8. **`success: true` means carrier accepted** — it does not mean the recipient received it (carrier delivery receipts are not implemented)
9. **`modem_id` (IMEI) is the hardware key** — use it in your modem inventory table; `sim_id` (IMSI) is the SIM key

---

## Environment Variables (Python side, for reference)

| Variable | Default | Description |
|---|---|---|
| `SMS_ENGINE_SERIAL_TIMEOUT` | `3` | Serial read timeout in seconds |
| `SMS_ENGINE_COMMAND_TIMEOUT` | `10` | Per-AT-command timeout |
| `SMS_ENGINE_SEND_TIMEOUT` | `30` | Full send sequence timeout |
| `SMS_ENGINE_PORT` | `8000` | HTTP port (production uses 9000) |
| `SMS_PYTHON_API_TOKEN` | `` | Shared secret — auth disabled if unset |

---

## Python Engine Status

| Feature | Status |
|---|---|
| Sysfs modem discovery | ✅ Production ready |
| Multi-modem support | ✅ Tested with 5 simultaneous modems |
| SMS send with AT commands | ✅ Tested, confirmed delivered |
| `error_layer` classification | ✅ hardware / modem / network / unknown |
| CMS/CME error code passthrough | ✅ Full numeric codes |
| `meta` echo in all responses | ✅ Confirmed |
| `message_id` echo | ✅ Confirmed |
| `modem_id` in health endpoint | ✅ Confirmed |
| Fast-fail on network errors | ✅ No wasted retry on carrier reject |
| Warm registry refresh | ✅ No serial I/O on TTL unless port disappears |
| API authentication (`X-Gateway-Token`) | ✅ Live-proven — 2026-04-06 |
| Discovery parallel probing | ✅ All modems probed concurrently — 2026-04-10 |
| Discovery bounded timeout | ✅ Hard 12s per-modem wall-clock limit — 2026-04-10 |
| Partial discovery results | ✅ `probe_error` per modem, one bad modem does not block others — 2026-04-10 |
| `send_ready` field | ✅ Explicit send-readiness flag per modem row — 2026-04-10 |
| `identifier_source` field | ✅ Explicit SIM identity source (`"imsi"` vs `"fallback_device_id"`) — 2026-04-10 |
| Per-modem send lock | ❌ Concurrent sends to same modem may collide (Task 012B) |
