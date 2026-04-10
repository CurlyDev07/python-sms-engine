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

## [2026-04-10] – Discovery Timeout Fix: Parallel Probing + Bounded Timeout

### Fixed
- **Discovery hang bug**: One stuck modem serial probe could block the entire `/modems/discover` response indefinitely — request would hang until client timeout with no data returned
- Root cause: `serial.Serial()` open on Linux tty can block without a wall-clock bound when a USB device is transitioning or held by a ghost process; probes were sequential so one blocked all others

### Changed
- Modem probes now run in parallel via `ThreadPoolExecutor` — all N modems probed concurrently, not sequentially
- Hard per-modem wall-clock timeout (`PROBE_TIMEOUT_S = 12.0s`) enforced via `futures_wait(timeout=...)`; timed-out probes are marked and returned rather than blocking the response
- `serial.Serial()` now opened with `exclusive=True` — raises `BlockingIOError` immediately if another process holds the port, instead of blocking indefinitely
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