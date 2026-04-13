# Inbound SMS Listener — Design Document

**Status: Implemented and live as of 2026-04-14**

---

## Architecture

- **Listener model**: Push-based via `AT+CNMI=2,2,0,0,0` per active modem port (near-instant, no polling)
- **Python boundary**: Transport/execution only — no tenant logic, no Laravel DB lookups
- **Webhook target**: `POST /api/gateway/inbound` on Laravel
- **Identity contract**: Python sends `runtime_sim_id` (IMSI). Laravel resolves to `tenant_sims.id` internally.
- **Reliability**: ACK-gated delete — spool first, delete from SIM, deliver to Laravel, mark delivered on `ok:true`
- **Source of record**: Laravel DB. Python spool is a temporary reliability buffer only.

---

## End-to-End Flow

```
Customer sends SMS reply
      ↓
   Carrier delivers to SIM
      ↓
   Modem fires unsolicited "+CMT:" over serial  ← AT+CNMI configured at startup
      ↓
   InboundListener thread (one per modem port)
      ↓
   1. Parse +CMT → extract from_number, message, timestamp
   2. Convert modem timestamp (YY/MM/DD,HH:MM:SS±QQ) to ISO8601
   3. Check recent-duplicate guard (same sim+from+message within 30s → skip)
   4. Generate idempotency_key (UUID)
   5. Write to local SQLite spool (durable)         ← safe before deleting from SIM
   6. AT+CMGDA=6 → delete from SIM                 ← SIM storage freed, spool has it
   7. POST to Laravel /api/gateway/inbound
      ↓ 200 + {"ok":true}
   8. Mark spool record as delivered
      ↓ non-200 / ok!=true / network error
   8. Retry with exponential backoff (from spool)
```

---

## Inbound Webhook Payload

`POST /api/gateway/inbound`

```json
{
    "idempotency_key": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "runtime_sim_id":  "515039219149367",
    "customer_phone":  "+639171234567",
    "message":         "Hello this is a reply",
    "received_at":     "2026-04-13T21:00:00+08:00"
}
```

| Field | Type | Description |
|---|---|---|
| `idempotency_key` | UUID string | Stable across retries — Laravel uses this to dedupe |
| `runtime_sim_id` | string | IMSI of the receiving SIM — Laravel resolves to `tenant_sims.id` |
| `customer_phone` | string | Sender phone number in E.164 format as reported by carrier |
| `message` | string | Raw SMS body |
| `received_at` | ISO 8601 | Modem-reported timestamp, converted to ISO8601 |

### ACK contract

Laravel must return HTTP 200 **and** `{"ok": true}` to ACK. Any other response triggers retry.

```json
{"ok": true, "inbound_message_uuid": "...", "idempotency_key": "...", "queued_for_relay": true}
```

If Laravel returns HTTP 200 but `ok` is not `true` (e.g. `{"ok":false,"error":"validation_failed"}`), Python treats this as NOT delivered and retries with backoff.

---

## Components

### 1. `inbound_spool.py` — SQLite spool

Durable buffer between modem receipt and confirmed Laravel delivery.

**Schema:**
```sql
CREATE TABLE inbound_spool (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    runtime_sim_id  TEXT    NOT NULL,
    from_number     TEXT    NOT NULL,
    message         TEXT    NOT NULL,
    received_at     TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',   -- pending | delivered | abandoned
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    created_at      TEXT    NOT NULL
);
```

**Operations:**
- `insert()` — write on receipt, before SIM delete
- `mark_delivered(idempotency_key)` — called after Laravel `ok:true` ACK
- `record_attempt(idempotency_key)` — increment attempt counter before each POST
- `get_pending()` — all undelivered records for retry loop
- `is_recent_duplicate(sim, from, message)` — guard against drain/push race delivering same SMS twice

**File location:** `inbound_spool.db` in working directory (not committed to git)

---

### 2. `inbound_listener.py` — Per-modem serial listener

One thread per active modem port. Runs for the lifetime of the process.

**Startup sequence per modem:**
```
open serial port (1s read timeout for stop_event checking)
AT+CMGF=1             ← text mode
AT+CNMI=2,2,0,0,0     ← push unsolicited +CMT to serial immediately
AT+CMGL="ALL"         ← drain any messages already stored on SIM
  → process each stored message → spool → delete by index (AT+CMGD=N)
enter readline loop
```

**Read loop:**
```
while running:
    line = serial.readline()       ← 1s timeout, loops to check stop_event
    if line matches +CMT header:
        pending_cmt_from, pending_cmt_time = parse(line)
    elif pending_cmt_from is not None:
        message_body = line
        handle_inbound(from, body, convert_timestamp(pending_cmt_time))
        clear pending state
```

**Duplicate guard:** Before spooling, `is_recent_duplicate()` is checked. If the same (sim_id, from, message) was spooled within the last 30 seconds, the message is skipped with `INBOUND_DUPLICATE_SKIPPED` log. This handles the drain/push race where a stored message is both drained at startup AND arrives via +CMT push.

**SIM deletion order (most to least supported):**
1. `AT+CMGDA=6` — numeric form, most universal
2. `AT+CMGDA="DEL ALL"` — string form fallback
3. `AT+CMGD=1,4` — delete all flag fallback

**`+CMT:` format (two-line unsolicited):**
```
+CMT: "+639171234567","","26/04/13,21:00:00+32"
Hello this is a reply
```

**Modem timestamp format:** `YY/MM/DD,HH:MM:SS±QQ` where `QQ` is timezone offset in quarters of an hour. Python converts to ISO8601 before storing/sending (`26/04/13,21:00:00+32` → `2026-04-13T21:00:00+08:00`).

**Auto-restart:** On serial error, the session closes and restarts after 10 seconds. Pending spool records are still retried by `InboundRetryWorker` during the downtime.

---

### 3. `inbound_webhook.py` — HTTP client with retry

Delivers spool records to Laravel with exponential backoff.

**Retry policy:**
```
Attempt 1: immediate
Attempt 2: 5s delay
Attempt 3: 15s delay
Attempt 4: 60s delay
Attempt 5+: 300s delay (cap)
```

Max attempts: configurable via `SMS_ENGINE_INBOUND_RETRY_MAX` (default: 10).

A background `InboundRetryWorker` thread wakes every 30s and re-delivers any `status=pending` spool records that haven't been ACKed and are past their backoff delay.

**Delivery success criteria:**
- HTTP status is 2xx, AND
- Response body parses as JSON, AND
- `response["ok"] == True`

**Structured log events:**
```
INBOUND_WEBHOOK_REQUEST   key=... payload_keys=[...]
INBOUND_WEBHOOK_RESPONSE  key=... status=... ok=...
INBOUND_ACK_FALSE         key=... status=... body=...   ← 2xx but ok!=true
INBOUND_DELIVERED         key=... sim=... from=...      ← only on real success
INBOUND_DELIVERY_FAILED   key=... attempt=... next_retry_in=...
INBOUND_DELIVERY_ABANDONED key=... attempts=...         ← max attempts reached
```

---

### 4. `app.py` — Startup integration

On startup, after modem discovery:
```python
spool = InboundSpool()

retry_worker = InboundRetryWorker(spool=spool, webhook_url=..., max_attempts=...)
retry_worker.start()

for modem in registry.get_all():
    port = modem.get("port")
    sim_id = modem.get("sim_id")
    if not port or not sim_id:
        continue
    listener = InboundListener(port=port, runtime_sim_id=sim_id, spool=spool, ...)
    listener.start()
```

No changes to any existing endpoints or send path. Inbound is fully independent of outbound.

---

### 5. Config entries (`config.py`)

```python
self.inbound_webhook_url = os.getenv("SMS_ENGINE_INBOUND_WEBHOOK_URL", "")
self.inbound_retry_max   = int(os.getenv("SMS_ENGINE_INBOUND_RETRY_MAX", "10"))
```

Add to `.env`:
```
SMS_ENGINE_INBOUND_WEBHOOK_URL=http://127.0.0.1:8081/api/gateway/inbound
SMS_ENGINE_INBOUND_RETRY_MAX=10
```

If `SMS_ENGINE_INBOUND_WEBHOOK_URL` is empty, messages are spooled but not delivered.

---

## Adding New Modems

Inbound listeners are started once at process startup. To pick up a newly plugged modem:

```bash
sudo systemctl restart sms-engine
```

This causes a ~2-3 second downtime. All 50+ modems are re-scanned and listeners re-launched. Any SMS that arrives during the gap is stored on the SIM and drained by `AT+CMGL="ALL"` at startup.

---

## Key Design Boundaries

| Concern | Owner |
|---|---|
| Parse `+CMT:` from serial | Python |
| Convert modem timestamp to ISO8601 | Python |
| Generate idempotency key | Python |
| Spool to SQLite | Python |
| Delete from SIM after spool | Python |
| POST to Laravel webhook | Python |
| Retry until ACK | Python |
| Resolve `runtime_sim_id` → `tenant_sims.id` | Laravel |
| Route reply to correct tenant/conversation | Laravel |
| Store final message record | Laravel DB |
| Deduplicate by `idempotency_key` | Laravel |

---

## Verification

```bash
# Test the webhook contract end-to-end without a real modem
python3 test_inbound_webhook.py --url http://127.0.0.1:8081/api/gateway/inbound

# Watch live inbound activity on server
sudo journalctl -u sms-engine -f | grep --line-buffered "INBOUND"

# Inspect spool state
sqlite3 inbound_spool.db "SELECT status, COUNT(*) FROM inbound_spool GROUP BY status;"
```
