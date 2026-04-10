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