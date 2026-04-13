# Inbound SMS Listener — Design Document

## Confirmed Direction

- **Listener model**: Push-based via `AT+CNMI=2,2,0,0,0` per active modem port (near-instant)
- **Python boundary**: Transport/execution only — no tenant logic, no Laravel DB lookups
- **Webhook target**: `POST /api/gateway/inbound` on Laravel
- **Identity contract**: Python sends `runtime_sim_id` (IMSI). Laravel resolves to `tenant_sims.id` internally.
- **Reliability**: ACK-gated delete with durable local spool + retry with backoff + idempotency key
- **Source of record**: Laravel DB. Python spool is a temporary reliability buffer only.

---

## End-to-End Flow

```
Customer sends SMS
      ↓
   Carrier delivers to SIM
      ↓
   Modem fires unsolicited "+CMT:" over serial  (AT+CNMI configured at startup)
      ↓
   InboundListener thread (one per modem port)
      ↓
   1. Parse +CMT → extract from, message, timestamp
   2. Generate idempotency_key (UUID)
   3. Write to local SQLite spool (durable)         ← safe before deleting from SIM
   4. AT+CMGD → delete from SIM                    ← SIM storage freed, spool has it
   5. POST to Laravel /api/gateway/inbound
      ↓ 200 ACK
   6. Mark spool record as delivered
      ↓ no ACK / error
   6. Retry with exponential backoff (from spool)
```

---

## Inbound Webhook Payload

`POST /api/gateway/inbound`

```json
{
    "idempotency_key": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "runtime_sim_id": "515039219149367",
    "from": "+639171234567",
    "message": "Hello this is a reply",
    "received_at": "2026-04-13T21:00:00+08:00"
}
```

| Field | Type | Description |
|---|---|---|
| `idempotency_key` | UUID string | Stable across retries — Laravel uses this to dedupe |
| `runtime_sim_id` | string | IMSI of the receiving SIM — Laravel resolves to `tenant_sims.id` |
| `from` | string | Sender phone number as reported by carrier |
| `message` | string | Raw SMS body |
| `received_at` | ISO 8601 | Timestamp modem reported for the message |

Laravel must return `HTTP 200` to ACK. Any non-200 triggers retry.

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
    status          TEXT    NOT NULL DEFAULT 'pending',   -- pending | delivered
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    created_at      TEXT    NOT NULL
);
```

**Operations:**
- `insert(record)` — write on receipt, before SIM delete
- `mark_delivered(idempotency_key)` — called after Laravel 200 ACK
- `get_pending()` — all undelivered records for retry loop

**File location:** `inbound_spool.db` in working directory (not committed to git)

---

### 2. `inbound_listener.py` — Per-modem serial listener

One thread per active modem port. Runs for the lifetime of the process.

**Startup sequence per modem:**
```
open serial port (serial_timeout=None — blocking read)
AT+CMGF=1       ← text mode
AT+CNMI=2,2,0,0,0   ← push unsolicited +CMT to serial immediately
AT+CMGL="ALL"   ← drain any messages that arrived before listener started
process any existing messages → spool → delete
enter blocking read loop
```

**Read loop:**
```
while running:
    line = serial.readline()        ← blocks until data arrives
    if line starts with "+CMT:":
        next_line = serial.readline()   ← the message body
        parse → spool → AT+CMGD → POST webhook
```

**`+CMT:` format:**
```
+CMT: "+639171234567","","26/04/13,21:00:00+32"
Hello this is a reply
```

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

Max attempts: configurable via `SMS_ENGINE_INBOUND_RETRY_MAX` (default: 10)

A background retry thread wakes every 30s and re-delivers any `status=pending` spool records that haven't been ACKed.

---

### 4. Changes to `app.py`

On startup, after modem discovery:
```python
# For each send-ready modem, start an inbound listener thread
for modem in registry.get_all():
    if modem.get("send_ready"):
        thread = InboundListener(port=modem["port"], sim_id=modem["sim_id"], ...)
        thread.start()
```

No changes to any existing endpoints or send path.

---

### 5. New config entries (`config.py`)

```python
self.inbound_webhook_url = os.getenv("SMS_ENGINE_INBOUND_WEBHOOK_URL", "")
self.inbound_retry_max   = int(os.getenv("SMS_ENGINE_INBOUND_RETRY_MAX", "10"))
```

Add to `.env`:
```
SMS_ENGINE_INBOUND_WEBHOOK_URL=http://your-laravel-app.com/api/gateway/inbound
```

---

## Files to Create

| File | Purpose |
|---|---|
| `inbound_spool.py` | SQLite spool — insert, mark_delivered, get_pending |
| `inbound_listener.py` | Per-modem thread — AT+CNMI config + +CMT parse loop |
| `inbound_webhook.py` | HTTP POST to Laravel + retry with backoff |

## Files to Modify

| File | Change |
|---|---|
| `app.py` | Launch listener threads on startup after modem discovery |
| `config.py` | Add `inbound_webhook_url`, `inbound_retry_max` |
| `.env` | Add `SMS_ENGINE_INBOUND_WEBHOOK_URL` |
| `docs/API.md` | Document inbound payload contract |
| `LARAVEL_INTEGRATION.md` | Document `/api/gateway/inbound` endpoint spec |

## Files NOT touched

- `at_client.py` — send path unchanged
- `sms_service.py` — send path unchanged
- `modem_detector.py` — discovery unchanged
- `modem_registry.py` — registry unchanged
- `schemas.py` — no new API schemas needed (inbound is outbound webhook, not an endpoint)

---

## Key Design Boundaries

| Concern | Owner |
|---|---|
| Parse `+CMT:` from serial | Python |
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

## Notes

- Listener threads share no state with the send path — inbound and outbound are fully independent
- If a modem is discovered after startup (e.g. SIM inserted later), a listener must be started for it at that point too
- The spool DB file must be excluded from git (add `inbound_spool.db` to `.gitignore`)
- If the engine restarts, the retry loop drains any `pending` spool records on startup before listening for new messages
