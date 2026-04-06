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