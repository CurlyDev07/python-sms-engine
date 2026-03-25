You are working on a Python-based SMS Execution Engine.

Follow ALL rules in /docs/SYSTEM.md and /docs/DECISIONS.md

---

SYSTEM ALIGNMENT RULE:

Before doing anything:

1. Identify current phase from ROADMAP.md
2. Ensure task belongs to that phase
3. Do NOT jump phases unless instructed

---

TASK:
[PUT TASK HERE]

---

CONTEXT:

- This system is EXECUTION LAYER ONLY
- Laravel Gateway handles:
  - queue
  - retry
  - failover
  - business logic

- Python Engine handles ONLY:
  - modem communication
  - AT commands
  - SIM → ttyUSB mapping
  - SMS execution

---

STRICT RULES:

- NO database
- NO queue logic
- NO retry logic
- NO business logic
- NO AI logic
- NO conversation logic
- NO cross-system coupling

- ALWAYS return standardized response:
  success / message_id / error / raw

- NEVER expose raw exceptions to API

---

ARCHITECTURE RULE:

Gateway = Control Plane  
Python = Execution Plane  

---

CODE REQUIREMENTS:

- Use FastAPI  [oai_citation:0‡Deepnote](https://deepnote.com/blog/ultimate-guide-to-fastapi-library-in-python?utm_source=chatgpt.com)
- Use pyserial for modem communication
- Use Pydantic models for validation
- Add structured logging
- Add type hints
- Handle timeouts safely

---

AFTER TASK YOU MUST:

1. Update CHANGELOG.md
2. Update ROADMAP.md (move progress)
3. If architecture changed → update SYSTEM.md
4. If decision made → update DECISIONS.md

---

SELF CHECK:

- Does this match current phase?
- Did I break execution-only rule?
- Did I add forbidden logic (retry, queue, AI)?
- Are docs consistent?

---

OUTPUT:

1. Phase: [which phase]
2. Feature: [what feature]
3. Modified files
4. Code summary
5. Docs updated