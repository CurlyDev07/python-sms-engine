You are reviewing an existing Python SMS Engine implementation.

Your job is to HARDEN and FIX issues.

---

CRITICAL CHECKS (DO NOT SKIP)

### 1. MODEM SAFETY

- Serial connection ALWAYS closed (finally block)
- No hanging read/write
- Timeouts enforced
- No infinite loops

---

### 2. AT COMMAND FLOW

Must strictly follow:

- AT → OK
- AT+CMGF=1 → OK
- AT+CMGS → wait for ">"
- send message + CTRL+Z
- wait for final response

Reject if any step fails

---

### 3. ERROR NORMALIZATION

Ensure ALL errors map to:

- SIM_NOT_MAPPED
- PORT_NOT_FOUND
- MODEM_OPEN_FAILED
- MODEM_TIMEOUT
- AT_NOT_RESPONDING
- CMGF_FAILED
- CMGS_PROMPT_FAILED
- SEND_FAILED
- UNKNOWN_ERROR

NO raw exception leakage

---

### 4. API RESPONSE CONSISTENCY

Every response MUST be:

{
  "success": boolean,
  "message_id": null,
  "error": string|null,
  "raw": object
}

---

### 5. SIM MAPPING SAFETY

- No hardcoded ttyUSB
- Always use config file
- Fail fast if sim_id not found

---

### 6. ARCHITECTURE COMPLIANCE

Ensure:

- No DB usage
- No retry logic
- No queue logic
- No business logic

---

### 7. LOGGING QUALITY

Ensure structured logs exist:

- SMS_SEND_ATTEMPT
- SMS_SEND_SUCCESS
- SMS_SEND_FAILED
- MODEM_HEALTH_CHECK

---

### 8. FASTAPI BEST PRACTICES

- Use Pydantic models  [oai_citation:1‡app-generator.dev](https://app-generator.dev/docs/technologies/fastapi/cheatsheet.html?utm_source=chatgpt.com)
- Validate input properly
- Proper status codes
- No blocking misuse in async endpoints

---

### 9. RESOURCE SAFETY

- No global serial reuse (for now)
- No memory leaks
- Safe exception handling

---

### OPTIONAL IMPROVEMENTS (APPLY IF SAFE)

- Add connectTimeout vs readTimeout separation
- Add retry inside AT step (NOT full SMS retry)
- Add debug-safe raw logging
- Improve error messages clarity

---

DO NOT:

- Change architecture
- Add new systems
- Add retry system
- Add queue system

---

OUTPUT:

1. Critical fixes applied
2. Improvements applied
3. Code changes summary
4. Any risks remaining

## SMS ENGINE FIX TEMPLATE

Problem:
- Describe issue (e.g. wrong port selection)

Constraints:
- Do NOT scan all ttyUSB
- Do NOT change identity model
- Preserve sysfs grouping

Fix Scope:
- Only modify relevant file

Validation:
- Must pass:
  - correct modem selection
  - no full scan
  - stable sim_id