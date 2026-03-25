# PYTHON SMS ENGINE – DECISIONS LOG

---

## Decision: Python as SMS Execution Layer

Date: 2026-03-25

Rule:
- All modem communication is handled by a Python service
- Laravel Gateway communicates via HTTP only

Reason:
- Python is better suited for hardware/serial communication
- Avoid blocking IO in Laravel
- Clean separation of control vs execution layers

Impact:
- Requires Python API service
- Adds network call between Gateway and modem layer
- Improves scalability and system stability

---

## Decision: FastAPI as Web Framework

Date: 2026-03-25

Rule:
- Use FastAPI for HTTP service

Reason:
- Lightweight and fast
- Easy async support
- Clean request/response validation via Pydantic

Impact:
- Simple API layer
- Easy local and production deployment via uvicorn

---

## Decision: pyserial for Modem Communication

Date: 2026-03-25

Rule:
- Use pyserial for USB modem communication

Reason:
- Standard library for serial communication
- Stable and widely supported
- Works directly with ttyUSB devices

Impact:
- Direct control over AT commands
- Requires careful timeout and error handling

---

## Decision: SIM Mapping via Configuration File

Date: 2026-03-25

Rule:
- sim_id → ttyUSB mapping is defined in config file (sim_map.json)

Reason:
- Avoid hardcoding ports in code
- Easy to update without redeploying logic
- Flexible for multi-modem setups

Impact:
- Requires config management
- Must validate mapping at runtime

---

## Decision: Stateless Execution Layer

Date: 2026-03-25

Rule:
- Python service MUST NOT store any data

Reason:
- Keep execution layer simple
- Avoid synchronization issues
- All state handled by Gateway

Impact:
- No database required
- All retry/queue handled externally

---

## Decision: No Retry Logic in Python

Date: 2026-03-25

Rule:
- Python engine performs a single send attempt only

Reason:
- Retry logic belongs to Gateway (control layer)
- Avoid duplicate SMS risks
- Maintain clear responsibility boundaries

Impact:
- Python returns failure immediately
- Gateway handles retry scheduling

---

## Decision: Standardized Response Format

Date: 2026-03-25

Rule:
- All responses must follow consistent structure:
  - success
  - message_id
  - error
  - raw

Reason:
- Align with Laravel SmsSendResult
- Simplify integration
- Enable consistent error handling

Impact:
- No raw exceptions exposed
- All errors normalized

---

## Decision: Short-Lived Modem Connections

Date: 2026-03-25

Rule:
- Open and close serial connection per request

Reason:
- Simpler implementation
- Avoid stale or locked ports
- Easier recovery from modem errors

Impact:
- Slight overhead per send
- More stable for initial version

---

## Decision: No Direct Hardware Access from Laravel

Date: 2026-03-25

Rule:
- Laravel MUST NOT access ttyUSB or AT commands directly

Reason:
- Enforce architecture boundary
- Prevent blocking issues
- Keep Gateway portable and scalable

Impact:
- All SMS sending must go through Python API
- Strong separation between layers

---

## Decision: Execution Layer Only

Date: 2026-03-25

Rule:
- Python service is strictly execution-only

It MUST NOT:
- contain business logic
- handle queue
- implement retry
- manage conversations
- process AI or automation

Reason:
- Maintain clean architecture
- Avoid duplication of logic
- Keep system maintainable

Impact:
- Gateway remains control layer
- Python remains hardware execution layer

---

## ARCHITECTURE PRINCIPLE

Gateway = Control Plane  
Python SMS Engine = Execution Plane  
Chat App = Intelligence Plane