# PYTHON SMS ENGINE – ROADMAP

---

## CURRENT PHASE
Phase 1 – Core SMS Execution

---

## DONE
- System architecture defined
- Execution layer separation from Gateway established

---

## IN PROGRESS
- None

---

## NEXT

### Phase 1 (Core SMS Execution)
- Basic FastAPI server setup
- POST /send endpoint
- SIM → ttyUSB mapping system
- AT command SMS sending (AT+CMGS)
- Standardized response format (success / error)
- Serial connection handling (open → send → close)
- Basic error normalization

---

### Phase 2 (Stability + Multi-Modem)
- Multi-modem support (USB hub handling)
- Modem health check endpoint (/modems/health)
- AT connectivity validation (AT → OK)
- Timeout handling improvements
- Robust error classification (modem vs network vs SIM issues)

---

### Phase 3 (Reliability + Observability)
- Structured logging (attempt / success / failure)
- Modem-level diagnostics (per SIM)
- Signal strength detection (optional AT commands)
- Debug-safe raw response capture
- Health monitoring improvements

---

## FUTURE

### Phase 4 (Performance Optimization)
- Persistent modem connections (connection pooling)
- Reduce open/close overhead
- Parallel modem handling
- Throughput optimization per SIM

---

### Phase 5 (Distributed Execution)
- Multi-node Python workers
- Gateway routing to multiple execution nodes
- Load distribution across modem clusters
- Fault isolation per node

---

### Phase 6 (Advanced Modem Intelligence)
- Auto modem detection (dynamic ttyUSB scan)
- SIM auto-mapping
- Advanced signal monitoring
- Carrier-aware optimizations

---

## NOTE

This system is the SMS Execution Layer only.

It MUST NOT include:
- queue logic
- retry logic
- business logic
- AI logic
- conversation handling

All orchestration is handled by the SMS Gateway (Laravel).

---

## ARCHITECTURE REMINDER

Gateway = Control Layer  
Python SMS Engine = Execution Layer  
Chat App = Intelligence Layer