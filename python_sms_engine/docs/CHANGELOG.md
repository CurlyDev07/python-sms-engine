# PYTHON SMS ENGINE – CHANGELOG

---

## [2026-03-25] – Initial System Setup

### Added
- System architecture documentation (execution layer role)
- Roadmap for SMS execution development phases
- Task tracker with core modem execution tasks
- Decisions log defining architecture boundaries and design rules

### Notes
- This system is the SMS Execution Layer
- It operates separately from the Laravel SMS Gateway
- No business logic, queue, or retry logic is implemented here
- All orchestration is handled by the Gateway

---

## [UNRELEASED] – Core SMS Execution (Planned)

### Planned
- FastAPI server setup
- /send endpoint implementation
- SIM → ttyUSB mapping system
- AT command SMS sending (AT+CMGS)
- Error normalization system
- Modem health check endpoint

### Notes
- First working version will focus on reliability over performance

## [SMS ENGINE REFACTOR]

### Added
- sysfs-based modem detection
- IMSI-based identity
- SMS capability check (AT+CPMS)
- fallback port logic

### Changed
- removed /dev/serial/by-id dependency
- replaced list registry with dict
- updated sim_id from int → string

### Fixed
- wrong modem mapping
- unstable identity
- excessive port scanning
- unreliable SMS routing

---

## [2026-04-04] – Laravel Contract Alignment (Task 012A)

### Added
- `meta` echo in all `/send` response paths (success, retry, fallback, failure)
- `message_id` echo from `meta.message_id` in top-level `SendResponse`
- `modem_id` (IMEI) field added to `/modems/health` schema and response
- Typed Pydantic response model for `/modems/discover` (`ModemsDiscoverResponse`)
- `LARAVEL_INTEGRATION.md` — authoritative Laravel↔Python API contract doc

### Notes
- Laravel integration proven live: send, discover, health all tested end-to-end
- Python remains execution-only — no business/retry/queue logic added

---

## [2026-04-10] – Discovery Contract Hardening: send_ready + identifier_source

### Added
- `send_ready` field on every `/modems/discover` modem row — explicit boolean telling consumers whether the row is safe to use as a `/send` target
- `identifier_source` field on every `/modems/discover` modem row — string enum telling consumers whether `sim_id` is a real telecom SIM identity or a fallback device identifier:
  - `"imsi"` — IMSI was successfully read from the SIM via `AT+CIMI`
  - `"fallback_device_id"` — IMSI was unavailable; `sim_id` fell back to ICCID, IMEI, or USB physical address
- `send_ready=True` only when all five conditions hold: `probe_error=null`, `at_ok=true`, `sim_ready=true`, `creg_registered=true`, and `identifier_source="imsi"`
- All existing fields preserved — fully backward-compatible, additive only

### Notes
- Consumers (e.g. Laravel) should use `send_ready` directly instead of re-deriving from individual flag combinations
- `identifier_source` makes explicit what was previously implicit — `sim_id` can be a fallback device identifier for unhealthy rows; that identifier is not safe for routing
- `detect_modems()` (startup/warm-cache path) is unchanged — it already filters to healthy-only

---

## [2026-04-10] – Probe Timeout Budget Fix: Caps + Remove exclusive=True

### Fixed
- **All probes returning PROBE_TIMEOUT**: Every `/modems/discover` row showed `PROBE_TIMEOUT after 12.0s` even when ports responded manually via minicom
- Root cause 1: `exclusive=True` (TIOCEXCL) — zombie threads from timed-out startup probes held open fds with TIOCEXCL set; subsequent probe opens blocked indefinitely instead of failing fast on this Linux kernel
- Root cause 2: Insufficient probe budget margin — `PROBE_TIMEOUT_S=12.0` minus `command_timeout=10.0` minus ~1s setup overhead left only ~1s margin; USB latency on any AT command pushed threads into `not_done`
- Root cause 3: `ser.read(256)` on Linux USB CDC-ACM can block beyond `serial_timeout` due to spurious `select()` wakeups; with `serial_timeout=3.0`, one blocked read consumed the entire remaining budget

### Changed
- Removed `exclusive=True` from `ModemATClient.open()` — TIOCEXCL no longer set; zombie probe threads from prior rounds cannot block new probe opens. ModemManager is masked so exclusive protection is not needed.
- `discover_all_modems()` and `detect_modems()` now cap timeouts at probe entry: `probe_serial_timeout = min(serial_timeout, 1.0)`, `probe_command_timeout = min(command_timeout, 5.0)`. Worst-case per-probe: ~1s open + ~5s AT = ~6s, well within 12s wall-clock deadline.
- Send path (`send_sms`) is unaffected — caps apply only inside probe entry points, not globally.

### Added
- Tests for timeout caps: `TestProbeTimeoutCaps` — verifies `_safe_probe` receives capped values in both `discover_all_modems` and `detect_modems`, and that values already within bounds are passed through unchanged.

---

## [2026-04-10] – Discovery Timeout Fix: Parallel Probing + Bounded Timeout

### Fixed
- **Discovery hang bug**: One stuck modem serial probe could block the entire `/modems/discover` response indefinitely — request would hang until client timeout with no data returned
- Root cause: `serial.Serial()` open on Linux tty can block without a wall-clock bound when a USB device is transitioning or held by a ghost process; probes were sequential so one blocked all others

### Changed
- Modem probes now run in parallel via `ThreadPoolExecutor` — all N modems probed concurrently, not sequentially
- Hard per-modem wall-clock timeout (`PROBE_TIMEOUT_S = 12.0s`) enforced via `futures_wait(timeout=...)`; timed-out probes are marked and returned rather than blocking the response
- `/modems/discover` now returns ALL detected ports including unhealthy and timed-out ones (previously returned only fully ready modems)
- `discover_all_modems()` added as a separate function for full-result discovery; `detect_modems()` (used by startup/warm-cache path) still filters to healthy-only

### Added
- `probe_error` field in `/modems/discover` response per modem — `null` when healthy, error string when probe failed or timed out
- `discover()` method on `ModemRegistry` — runs parallel probe, updates routing cache, returns all results
- Focused tests for: one-hung-modem scenario, all-timeout scenario, port-not-found fast-path, bounded response time, partial result correctness

### Notes
- `/modems/discover` is now safe to call from Laravel even when hardware is degraded — partial results are expected and normal, not an error state
- Unhealthy modems in the response should be inspected per `probe_error` field, not treated as a total discovery failure
- The warm-cache path (`detect_modems`) is unchanged — startup and TTL-based refresh still filter to healthy modems only
- Stuck probe threads may remain alive briefly after the timeout; they do not affect subsequent requests

---

## [2026-04-14] – Inbound SMS: Customer Reply Pipeline

### Added
- `inbound_listener.py` — one `InboundListener` thread per active modem port, configured via `AT+CNMI=2,2,0,0,0` for push-based unsolicited `+CMT:` notifications (near-instant, no polling)
- `inbound_spool.py` — thread-safe SQLite durability buffer; messages are spooled before SIM delete, ensuring no data loss on crash or retry
- `inbound_webhook.py` — HTTP delivery client with exponential backoff retry (`[0, 5, 15, 60, 300]` seconds); `InboundRetryWorker` background thread polls every 30s
- `test_inbound_webhook.py` — verification script to test the full request/response cycle against Laravel without a real modem
- `.env.example` — template with all environment variables documented
- Startup drain: `AT+CMGL="ALL"` on listener start processes messages that arrived before the listener was running

### Changed
- Inbound webhook payload uses `customer_phone` (not `from`) to match Laravel contract
- Delivery success now requires HTTP 2xx **and** `response["ok"] == True` — HTTP 200 with `ok:false` triggers retry
- SIM delete uses `AT+CMGDA=6` (numeric, more universal) with string and flag fallbacks
- Stored messages deleted by index (`AT+CMGD=N`) during drain — guaranteed per-message delete
- Modem GSM timestamps (`YY/MM/DD,HH:MM:SS±QQ`) converted to ISO8601 before delivery — fixes Laravel `validation_failed` on `received_at`
- Added recent-duplicate guard in spool — same (sim, from, message) within 30s is skipped to prevent drain/+CMT race delivering one SMS twice

### New config vars
```
SMS_ENGINE_INBOUND_WEBHOOK_URL   — Laravel /api/gateway/inbound endpoint
SMS_ENGINE_INBOUND_RETRY_MAX     — max delivery attempts (default: 10)
```

### New log events
```
INBOUND_RECEIVED          — message arrived from modem
INBOUND_DUPLICATE_SKIPPED — dedup guard fired (same message within 30s)
INBOUND_WEBHOOK_REQUEST   — about to POST to Laravel
INBOUND_WEBHOOK_RESPONSE  — Laravel responded (status + ok value)
INBOUND_ACK_FALSE         — 2xx but ok!=true (will retry)
INBOUND_DELIVERED         — Laravel ACKed ok:true (real success only)
INBOUND_DELIVERY_FAILED   — retry scheduled
INBOUND_DELIVERY_ABANDONED — max attempts reached
```

### Verified live
- `INBOUND_DELIVERED` fires only when Laravel returns `ok:true`
- Laravel DB row confirmed by `SELECT WHERE idempotency_key = '...'`
- Adding new modem: `sudo systemctl restart sms-engine` picks it up automatically

---

## [2026-04-18] – Send Latency Optimization: 26s → ~800ms

### Overview

End-to-end SMS send time reduced from **24–26 seconds** to **~800ms–1.2s** on the Python side through four sequential fixes. Each fix addressed a distinct root cause identified via `SEND_TIMING` structured logs.

### The Four Fixes

#### Fix 1 — Replace polling final-read loop with `_read_until()` (26s → 15s)

**Root cause:** After writing the SMS body, the original code did a fixed `sleep(1.5)` then polled `read_all()` every 200ms for up to 10 seconds regardless of when `+CMGS:` arrived. ATZ reset could also take up to `command_timeout` (10s) if the modem was slow.

**Fix:** Replaced polling loop with `_read_until(expected=["+CMGS:", "OK"])` which exits the moment the terminal token appears. Removed ATZ from the normal send path (kept only as fallback recovery). Removed inter-command sleeps (post-AT, post-ATE0, post-CMGF). Reduced port open stabilize delay 500ms → 200ms.

**Added:** `FAST_SEND_FLOW=true` feature flag — set `false` to revert to legacy path. Auto-fallback: if fast path fails, logs `fast_path_fallback=true` and retries via legacy.

**Added:** Structured `SEND_TIMING` log per send:
```
SEND_TIMING tx_id=... port=... sim_id=... result=success fast_path=True
  open_ms=205 setup_ms=9009 cmgs_prompt_ms=3003 final_wait_ms=3002 total_ms=15420
```

---

#### Fix 2 — Dedicate if03 to inbound listener, if02 exclusive to outbound (15s, root cause identified)

**Root cause:** The inbound listener (`AT+CNMI=2,2,0,0,0`) held `/dev/ttyUSBN` (if02) open permanently, blocking on `readline()`. When `send_sms()` opened its own fd to the same port and sent `AT\r`, the Linux kernel tty buffer delivered the modem's `OK` response to whichever fd's `read()` was already waiting — the listener's. `send_sms()` got nothing, waited the full `serial_timeout` (3s), retried. Result: `setup_ms=9009` (3 commands × 3s each).

**Fix:** Each USB modem exposes two AT-capable ports: if02 (primary) and if03 (fallback). Dedicated if03 exclusively to the inbound listener and if02 exclusively to outbound sends. Zero read competition.

**Changed** (`app.py`): `listen_port = modem.get("fallback_port") or port` — listener starts on if03 at every boot automatically.

**Changed** (`inbound_listener.py`): Removed cross-port lock from `_cmd()` — listener and sender are on separate ports with independent locks.

**Log change:**
```
INBOUND_LISTENER_LAUNCHED listen_port=/dev/ttyUSB3 send_port=/dev/ttyUSB2 sim=515020...
```

**Note:** Each USB modem exposes multiple ttyUSB ports per SIM. if02 = primary AT port. if03 = secondary AT port. Both talk to the same SIM. if03 was previously used only as a send fallback; it is now permanently assigned to the inbound listener.

---

#### Fix 3 — Persistent connection: keep if02 open between sends (15s → 6s)

**Root cause:** `send_sms()` opened the serial port fresh for every send. On Linux, when a serial port closes, the modem drops DTR and enters a low-power state. On the next open, the modem's AT processor takes ~3 seconds to wake up before it responds to commands. With 3 setup commands (AT + ATE0 + CMGF=1), that was 3 × 3s = `setup_ms=9009` every single send.

**Fix:** `ModemATClient.initialize()` opens the port once at startup and runs AT + ATE0 + CMGF=1 once. `ModemATClient.send_persistent()` skips all setup — goes straight to `AT+CMGS`. Port stays open for the lifetime of the process.

**Changed** (`sms_service.py`): `SmsService` holds one persistent `ModemATClient` per port. `warm_up()` called at startup initializes all known send ports. Lazy init for ports that appear later.

**Changed** (`app.py`): `service.warm_up(registry.get_all())` called after listeners start.

**Removed:** Fallback send path to `fallback_port` (if03) — if03 is now the listener port and cannot be used for sending.

**Result:** `open_ms=0 setup_ms=0` on every send after startup.

---

#### Fix 4 — Reduce `serial_timeout` from 3s to 0.2s (6s → ~800ms)

**Root cause:** Even with the persistent connection, `cmgs_prompt_ms=3003` and `final_wait_ms=3002` — both exactly `serial_timeout=3s`. Each `ser.read(256)` blocks for 3 seconds when no data is available. The modem's `>` prompt and `+CMGS:` response were arriving just after the 3-second window closed, causing one full missed read per phase: 3s wasted on the prompt + 3s wasted on the final response = 6s total.

**Fix:** Set `SMS_ENGINE_SERIAL_TIMEOUT=0.2` in `.env`. With 200ms reads, responses are caught within 200ms of arrival instead of up to 3s after.

**Result:**
```
cmgs_prompt_ms=200  final_wait_ms=600  total_ms=801
```

The `final_wait_ms=600–800ms` is the **real GSM carrier response time** — the actual time the carrier takes to confirm delivery. This is the hardware/network floor that no software change can reduce.

---

### Final Benchmarks

| Metric | Before | After |
|---|---|---|
| p50 send time (Python) | ~24s | ~900ms |
| p95 send time (Python) | ~26s | ~1.2s |
| `setup_ms` | 9,009ms | 0ms |
| `cmgs_prompt_ms` | 3,003ms | 200ms |
| `final_wait_ms` | 3,002ms | 600–800ms |
| `open_ms` | 205ms | 0ms |

End-to-end from chat app (Laravel → Python → modem → carrier → Python → Laravel response): approximately **2–4 seconds** depending on HTTP and Laravel processing overhead.

---

### Port Architecture (as of 2026-04-18)

Each USB GSM modem exposes multiple serial ports per SIM:

```
/dev/ttyUSBN   (if02) — outbound sends only (persistent, always open)
/dev/ttyUSBN+1 (if03) — inbound listener only (AT+CNMI, +CMT push)
```

This is auto-detected on every restart via sysfs. No manual configuration required when adding new modems.

---

### Configuration Change

```
SMS_ENGINE_SERIAL_TIMEOUT=0.2   ← was 3 (old default)
```

See `.env.example` for full explanation of this value.

---

### Known Limitations / Not Yet Tested

- **Inbound SMS end-to-end (Python → Chat App) not yet verified.** The inbound pipeline is implemented and Python → Laravel webhook delivery has been confirmed. However, the full round-trip from an inbound SMS arriving on the modem through to appearing in the Chat App UI has not been tested end-to-end. This should be validated before treating inbound as production-ready.

---

## [2026-04-18] – Discovery Stability: Hysteresis + Identity Recovery

### Added
- `_device_state` per-device persistent state in `ModemRegistry`, keyed by USB physical address (`device_id`, e.g. `3-7.4.4`)
- `_apply_hysteresis()` — smooths probe results across discover calls:
  - `effective_send_ready` only downgrades after 3 consecutive failures (`_DOWNGRADE_THRESHOLD = 3`)
  - A single transient bad probe does not flip the UI or remove the modem from the routing cache
  - `last_good_imsi` restored when a probe returns no identity — prevents `sim_id` flapping to `fallback_device_id`
- New fields on `/modems/discover` response per modem:
  - `realtime_probe_ready` — raw single-probe result (strict)
  - `effective_send_ready` — smoothed result after hysteresis (use this for UI/routing)
  - `identifier_source_confidence` — `high` (fresh IMSI) / `medium` (recovered from cache) / `low` (never seen)
  - `readiness_reason_code` — human-readable code when not ready (`PROBE_TIMEOUT`, `AT_NOT_RESPONDING`, `SIM_NOT_READY`, `CREG_NOT_REGISTERED`, `IMSI_UNAVAILABLE`)
  - `probe_timestamp`, `consecutive_probe_failures`, `last_good_probe_at`, `last_good_imsi` — diagnostic timeline fields

### Changed
- `discover()` routes cache updated using `effective_send_ready` (not raw `send_ready`) — modems survive brief probe failures without being dropped from routing

### New log events
```
MODEM_READY_STATE_CHANGED   — effective_send_ready flipped (with reason + failure count)
IDENTIFIER_SOURCE_CHANGED   — IMSI changed between probes (SIM swap detection)
IDENTIFIER_RECOVERED_FROM_CACHE — last_good_imsi restored after probe returned no identity
```

---

## [2026-04-19] – Modem Stability: Watchdog + USB Autosuspend Fix + AT Injection Fix

### Fixed

#### AT Command Injection into SMS Body
- **Root cause:** `/modems/discover` was being called automatically every ~60 seconds by Laravel. Each call opened a second file descriptor on if02 (the persistent connection port) without acquiring the port lock. If a send was in progress, the probe's `AT+CPIN?` and `AT+CREG?` bytes landed inside the open `AT+CMGS` body window — the modem treated them as SMS body text. Recipients received messages like `Hello AT+CPIN?AT+CREG?`.
- **Fix 1 — Port lock in probe:** `modem_detector._probe_port()` now acquires `get_port_lock(port)` before opening if02. Probe and send can never overlap.
- **Fix 2 — Close before probe:** `/modems/discover` endpoint now calls `service.close_all_clients()` before probing and `service.warm_up()` after. Eliminates double file descriptor entirely.
- **Fix 3 — DISCOVER_ENABLED flag:** Added `DISCOVER_ENABLED=false` env var to disable live probing without code changes. When disabled, returns cached registry state instantly. Used to stop the automatic discover calls from Laravel while root cause was investigated.

#### Inbound SMS Not Detected on if03
- **Root cause:** `AT+CNMI=2,2,0,0,0` (push mode) only delivers `+CMT` unsolicited notifications to if02 (primary port). if03 (secondary port) never receives them — the listener was READY but deaf.
- **Fix 1 — Switch to polling:** Removed `+CMT` wait loop from `InboundListener`. Now polls `AT+CMGL="ALL"` every 1 second on if03. Messages detected within ~1s of arrival.
- **Fix 2 — Store to SIM:** Added `AT+CNMI=2,1,0,0,0` to `initialize()` on if02. `mt=1` stores inbound SMS to SIM instead of pushing directly as `+CMT`. if03 polling finds them via `AT+CMGL`.

#### Abandoned Spool Records Logging Forever
- **Root cause:** `deliver_one()` returned `False` when `attempts >= max_attempts` but never updated the spool status. `get_pending()` kept returning the same records, so `INBOUND_DELIVERY_ABANDONED` was logged every 30s indefinitely.
- **Fix:** Added `InboundSpool.mark_abandoned()`. `deliver_one()` now calls it when max attempts reached — record status set to `abandoned`, excluded from future `get_pending()` results.

### Added

#### Modem Watchdog (`modem_watchdog.py`)
- New `ModemWatchdog` background thread, started at startup after `warm_up()`
- Pings each persistent connection with `AT` every 30 seconds
- Acquires port lock before pinging — never races with active sends
- On ping failure: closes and reinitializes the persistent connection automatically
- On reinit failure: logs `WATCHDOG_RECOVERY_FAILED` alert — send will attempt lazy reinit
- Skips ports where lock is busy after 5s timeout (defers to next 30s cycle)

```
New log events:
WATCHDOG_STARTED          — watchdog thread launched, interval logged
WATCHDOG_PING_ALL         — each cycle: modem count + known ports
WATCHDOG_OK               — AT ping succeeded for port/sim
WATCHDOG_SKIP             — port lock busy, deferred to next cycle
WATCHDOG_FAIL             — AT ping failed, reinit triggered
WATCHDOG_RECOVERED        — reinit succeeded after failure
WATCHDOG_RECOVERY_FAILED  — reinit also failed, alert logged
```

#### USB Autosuspend Disabled (Server Config)
- Linux was suspending Quectel EC25 modems after 2 seconds of inactivity — modems silently disappeared from `/dev/ttyUSBx`
- Added udev rule: `/etc/udev/rules.d/99-quectel.rules`
  ```
  ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="2c7c", ATTR{power/autosuspend}="-1"
  ```
- Also applied immediately via sysfs (no reboot required)
- All 3 Quectel modems now show `autosuspend=-1` permanently

### Verified Live (2026-04-19)
- All 3 modems (`ttyUSB2`, `ttyUSB6`, `ttyUSB10`) showing `WATCHDOG_OK` every 30s
- Inbound SMS received → spooled → delivered to Laravel in ~100ms
- No `MODEM_CLIENTS_CLOSED_FOR_PROBE` interference with sends

### Known Issues
- `/modems/discover` disabled (`DISCOVER_ENABLED=false`) — Laravel is calling it automatically every ~60s which causes persistent connections to close and reopen. Root cause on Laravel side needs investigation and the scheduler/cron calling discover should be identified and stopped or changed to use `/modems/health` instead.
- Inbound SMS → Chat App UI not yet appearing. Python → Laravel webhook confirmed working (`ok:true`). Suspected issue: Laravel's relay/broadcast queue (`queued_for_relay:true`) is not being processed.

---

## [2026-04-20] – /modems/health Now Returns Live Watchdog State

### Changed

`/modems/health` previously returned stale data from the startup registry probe — data could be hours old with no indication of current modem state. It now returns live watchdog data, refreshed every 30 seconds.

**Old contract (removed):**
```json
{ "sim_id": "...", "modem_id": "...", "port": "...", "reachable": true, "at_ok": true }
```

**New contract:**
```json
{
  "sim_id": "515039219149367",
  "modem_id": "866358071697796",
  "port": "/dev/ttyUSB6",
  "alive": true,
  "last_ping_at": "2026-04-19T11:53:04.272070+00:00",
  "last_ping_ok": true,
  "consecutive_failures": 0,
  "send_ready": true
}
```

**Field changes:**

| Old | New | Notes |
|---|---|---|
| `reachable` | `alive` | Same meaning, now based on live AT ping |
| `at_ok` | `last_ping_ok` | Same meaning, timestamped |
| `sim_ready` | removed | Covered by `send_ready` |
| `creg_registered` | removed | Covered by `send_ready` |
| `signal` | removed | Not tracked by watchdog |
| — | `last_ping_at` | ISO8601 timestamp of last ping |
| — | `consecutive_failures` | Count of consecutive failed pings |
| `send_ready` | `send_ready` | Unchanged — use this for routing |

**Routing guidance:**
- `send_ready == true` → safe to route sends to this `sim_id`
- `consecutive_failures >= 3` → consider alerting operator
- Data is at most 30s stale (watchdog ping interval)
- Safe to poll anytime — no hardware interaction, reads from memory only

### Changed
- `ModemWatchdog` now tracks `_status` dict per port — updated after every ping
- `ModemWatchdog.get_status()` exposes live state for `/modems/health`
- `ModemWatchdog.run()` pings immediately at startup — health endpoint populated within seconds, not after first 30s cycle
- `ModemHealthItem` schema updated — old fields removed, new watchdog fields added
- `/modems/health` handler reads from `app.state.modem_watchdog.get_status()` directly

---

## [2026-04-18] – Per-Port Send Lock: Prevents AT Command Injection

### Fixed
- **AT command text appearing in outbound SMS body** (e.g. `AT+CMGF=1` inserted into the message)
- **Root cause:** Two open file descriptors on the same tty (inbound listener fd + send fd). During an inbound listener session restart, `AT+CMGF=1` was written while `send_sms()` was inside the AT+CMGS text-entry window. The modem treated the AT command bytes as message body.

### Added
- Per-port `threading.Lock` in `at_client.py` (`_port_locks` dict, `get_port_lock(port)`)
- `send_sms()` acquires the lock before `open()`, holds it for the entire transaction (open → close)
- `InboundListener._cmd()` acquires the lock around every write (since superseded by the if02/if03 port split — lock now primarily guards against concurrent send attempts)

---

## [2026-04-06] – Phase 2 Hardening: Python API Authentication

### Added
- Shared-secret token auth on all Laravel-facing endpoints
- Header: `X-Gateway-Token`
- Env var: `SMS_PYTHON_API_TOKEN`
- Returns `401 {"success": false, "error": "UNAUTHORIZED"}` on missing/wrong token
- Auth is disabled when `SMS_PYTHON_API_TOKEN` is unset (safe local dev default)

### Protected endpoints
- `POST /send`
- `GET /modems/discover`
- `GET /modems/health`
- `GET /modems/available`
- `GET /modems/summary`
- `GET /modems/debug`
- `POST /dev/stub/send-network-fail`

### Intentionally unprotected
- `GET /health` — liveness probe, no data exposed

### Notes
- Authenticated Laravel→Python→modem flow live-proven
- Implementation: FastAPI `Depends(_require_token)` per-route, no global middleware