# PYTHON SMS ENGINE – TASK TRACKER

---

## TASK 001 – FastAPI Server Setup

Status: TODO

Goal:
- Initialize Python SMS Engine service

Scope:
- Setup FastAPI app
- Configure uvicorn server
- Basic project structure
- Health endpoint (/health)

Result:
- Pending

---

## TASK 002 – SIM Mapping System

Status: TODO

Goal:
- Map sim_id to modem port (ttyUSB)

Scope:
- Load sim_map.json config
- Validate sim_id existence
- Resolve sim_id → ttyUSB path
- Handle missing mapping errors

Result:
- Pending

---

## TASK 003 – AT Command Client

Status: TODO

Goal:
- Implement low-level modem communication

Scope:
- Open serial port via pyserial
- Send AT command
- Wait for OK response
- Handle timeouts
- Close connection safely

Result:
- Pending

---

## TASK 004 – SMS Sending Flow (AT+CMGS)

Status: TODO

Goal:
- Send SMS using modem

Scope:
- Set SMS mode (AT+CMGF=1)
- Send AT+CMGS command
- Wait for prompt (>)
- Send message + CTRL+Z
- Parse modem response
- Return success/failure

Result:
- Pending

---

## TASK 005 – /send API Endpoint

Status: TODO

Goal:
- Expose SMS send API for Gateway

Scope:
- Validate request payload
- Resolve sim_id → port
- Call SMS sending service
- Return standardized response (success/error/raw)
- Handle exceptions safely

Result:
- Pending

---

## TASK 006 – Error Handling + Normalization

Status: TODO

Goal:
- Standardize all modem errors

Scope:
- Map exceptions to stable error codes:
  (SIM_NOT_MAPPED, MODEM_TIMEOUT, SEND_FAILED, etc.)
- Prevent raw stack trace leaks
- Ensure consistent API response format

Result:
- Pending

---

## TASK 007 – Modem Health Check

Status: TODO

Goal:
- Monitor modem availability

Scope:
- GET /modems/health endpoint
- Send AT command per modem
- Detect reachable / not reachable
- Return structured health response

Result:
- Pending

---

## TASK 008 – Logging System

Status: TODO

Goal:
- Add structured observability logs

Scope:
- Log SMS_SEND_ATTEMPT
- Log SMS_SEND_SUCCESS
- Log SMS_SEND_FAILED
- Log MODEM_HEALTH_CHECK
- Include sim_id, port, phone, error

Result:
- Pending

---

## TASK 009 – Multi-Modem Support

Status: TODO

Goal:
- Support multiple USB modems

Scope:
- Handle multiple ttyUSB ports
- Independent SIM execution
- Safe parallel handling
- No shared state conflicts

Result:
- Pending

---

## TASK 010 – Configuration System

Status: TODO

Goal:
- Centralize environment/config management

Scope:
- ENV variables (timeouts, ports, host)
- Config file for SIM mapping
- Config loader module
- Safe defaults

Result:
- Pending

---

## NOTE

This system is execution-only.

DO NOT add:
- queue logic
- retry logic
- business logic
- AI logic
- conversation handling

All orchestration is handled by the SMS Gateway (Laravel).

---

## DESIGN RULE

Python SMS Engine must remain:

- stateless
- lightweight
- hardware-focused
- easily replaceable

## CURRENT TASK — SMS ENGINE

Status: ✅ STABLE BASE COMPLETE

Done:
- sysfs detection
- IMSI identity
- registry optimization
- retry + fallback

Next:
- Redis queue integration
- multi-worker sending
- throughput optimization