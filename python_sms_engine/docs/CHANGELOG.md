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