# Python SMS Engine

Execution plane for SMS delivery using Quectel EC25 USB modems on Linux.

- **Laravel SMS Gateway** = control plane (queueing, business logic, database)
- **Python SMS Engine** = execution plane (AT commands, modem I/O, serial communication)

---

## Project Structure

| File | Purpose |
|---|---|
| `app.py` | FastAPI app, startup, endpoints |
| `config.py` | Environment config |
| `schemas.py` | Request/response models |
| `modem_detector.py` | Sysfs-based USB modem discovery |
| `modem_registry.py` | In-memory modem cache with warm refresh |
| `modem_manager.py` | Health checks and modem summaries |
| `at_client.py` | Serial port + AT command client |
| `sms_service.py` | SMS send orchestration, error classification |

---

## Requirements

- Python 3.10+
- Quectel EC25 (or compatible) USB modems on `/dev/ttyUSB*`
- Linux with sysfs (`/sys/class/tty/`)
- ModemManager **must be disabled** (see below)

---

## First-Time Setup

```bash
cd python_sms_engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Disable ModemManager (required)

ModemManager is a Linux system daemon that auto-claims all USB modems. It must be permanently disabled or it will block all serial port access.

```bash
sudo systemctl stop ModemManager
sudo systemctl mask ModemManager
```

Verify it's gone:
```bash
systemctl status ModemManager
# Should show: masked
```

### Check port permissions

Your user must be in the `dialout` group:
```bash
sudo usermod -aG dialout $USER
# Log out and back in for this to take effect
```

---

## Running the Service

Always use the venv's uvicorn, not the system one:

```bash
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 9000
```

Or directly without activating:
```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 9000
```

---

## Restart Procedure (clean restart)

Use this when updating code or after any crash:

```bash
find . -name "*.pyc" -delete && find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
fuser -k 9000/tcp
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 9000
```

---

## API Endpoints

### Health check
```bash
curl -s http://127.0.0.1:9000/health | python3 -m json.tool
```
```json
{
    "success": true,
    "service": "python_sms_engine",
    "status": "ok"
}
```

---

### Discover modems (force full rescan)
```bash
curl -s http://127.0.0.1:9000/modems/discover | python3 -m json.tool
```
```json
{
    "success": true,
    "modems": [
        {
            "sim_id": "515039219149367",
            "modem_id": "866358071697796",
            "device_id": "3-7.4.4",
            "port": "/dev/ttyUSB2",
            "fallback_port": "/dev/ttyUSB3",
            "interface": "if02",
            "at_ok": true,
            "sim_ready": true,
            "creg_registered": true,
            "signal": "+CSQ: 20,99",
            "imsi": "515039219149367",
            "iccid": "89630323255005160625",
            "imei": "866358071697796"
        }
    ]
}
```

| Field | Description |
|---|---|
| `sim_id` | IMSI — used to route SMS requests |
| `modem_id` | IMEI — hardware identity, stays with device |
| `device_id` | USB physical address (sysfs) |
| `port` | Primary serial port (if02) |
| `fallback_port` | Fallback serial port (if03) |
| `signal` | Signal strength from AT+CSQ |

---

### Available modems (ready for SMS)
```bash
curl -s http://127.0.0.1:9000/modems/available | python3 -m json.tool
```
Returns only modems where `at_ok=true`, `sim_ready=true`, `creg_registered=true`.

---

### Modem health
```bash
curl -s http://127.0.0.1:9000/modems/health | python3 -m json.tool
```
```json
{
    "success": true,
    "modems": [
        {
            "sim_id": "515039219149367",
            "port": "/dev/ttyUSB2",
            "reachable": true,
            "at_ok": true
        }
    ]
}
```

---

### Modem summary
```bash
curl -s http://127.0.0.1:9000/modems/summary | python3 -m json.tool
```
```json
{
    "success": true,
    "summary": {
        "total": 5,
        "online": 4,
        "offline": 1
    }
}
```

---

### Debug dump (full raw modem state)
```bash
curl -s http://127.0.0.1:9000/modems/debug | python3 -m json.tool
```

---

### Send SMS

**Request:**
```bash
curl -s -X POST http://127.0.0.1:9000/send \
  -H "Content-Type: application/json" \
  -d '{"sim_id":"515039219149367","phone":"+639550090156","message":"Hello"}' \
  | python3 -m json.tool
```

**Success response:**
```json
{
    "success": true,
    "message_id": null,
    "error": null,
    "raw": {
        "sim_id": "515039219149367",
        "modem_id": "866358071697796",
        "port": "/dev/ttyUSB2",
        "status": "success",
        "modem_response": "OK\r\n\nATE0\r\r\nOK\r\n\n\r\nOK\r\n\n\r\n> \n+CMGS: 113\r\n\r\nOK"
    }
}
```

**Failure response:**
```json
{
    "success": false,
    "message_id": null,
    "error": "SEND_FAILED",
    "raw": {
        "sim_id": "515039219149367",
        "modem_id": "866358071697796",
        "port": "/dev/ttyUSB2",
        "error_layer": "network",
        "cms_error_code": 350,
        "cme_error_code": null,
        "modem_response": "..."
    }
}
```

---

## Error Reference

### `error` codes

| Code | Meaning |
|---|---|
| `SIM_NOT_MAPPED` | No modem found for given sim_id |
| `PORT_NOT_FOUND` | Serial port file does not exist |
| `MODEM_OPEN_FAILED` | Could not open port (busy or permission denied) |
| `MODEM_TIMEOUT` | No response within timeout |
| `AT_NOT_RESPONDING` | Modem did not respond to AT |
| `CMGF_FAILED` | Failed to set text mode |
| `CMGS_PROMPT_FAILED` | Modem rejected AT+CMGS (no `>` prompt) |
| `SEND_FAILED` | Message rejected by modem or network |
| `UNKNOWN_ERROR` | Unexpected error |

### `error_layer` classification

| Layer | Cause | When |
|---|---|---|
| `hardware` | Port dead, modem unplugged, timeout | `cms_code=null`, `cme_code=null`, hardware error code |
| `modem` | SIM not inserted, PIN required, SIM failure | `cme_code` is set |
| `network` | No credit, invalid number, carrier reject | `cms_code` is set |
| `unknown` | Unclassified | Neither code is set |

### Common CMS error codes (network layer)

| Code | Meaning |
|---|---|
| 27 | Destination unreachable |
| 38 | Network out of order |
| 50 | No credit / insufficient funds |
| 350 | Invalid destination number |

### Common CME error codes (modem layer)

| Code | Meaning |
|---|---|
| 10 | SIM not inserted |
| 11 | SIM PIN required |
| 13 | SIM failure |
| 14 | SIM busy |

---

## Send Retry Logic

1. **Primary port (if02)** — first attempt
2. **Retry same port** — after 0.5s (hardware errors only)
3. **Fallback port (if03)** — if retry also fails (hardware errors only)

Network/modem errors (`cms_code` or `cme_code` set) skip retry immediately — a different port will not fix a carrier rejection or invalid number.

---

## Debugging

### Check what's holding a port
```bash
fuser -v /dev/ttyUSB*
```

### Kill a specific process holding a port
```bash
kill -9 <PID>
```

### Test a port manually with minicom
```bash
sudo minicom -D /dev/ttyUSB2 -b 115200
```
Inside minicom, press `Ctrl+A E` to enable echo. Then type:
```
ATZ
AT+CPIN?
AT+CREG?
AT+CSQ
AT+CMGF=1
AT+CMGS="+639XXXXXXXXX"
```
Type message, then press `Ctrl+Z` to send. Exit with `Ctrl+A X`.

### Check ModemManager status
```bash
systemctl status ModemManager
```

### Check if port is truly free
```bash
fuser /dev/ttyUSB2
# No output = port is free
```

### Startup log format (healthy)
```
[SYSFS SCAN] 20 ttyUSB devices found
[SYSFS] ttyUSB2 -> 3-7.4.4 if02 (primary)
[SYSFS] ttyUSB3 -> 3-7.4.4 if03 (fallback, not probed)
[PROBE] 3-7.4.4 -> /dev/ttyUSB2 (fallback: /dev/ttyUSB3)
[TRY] 3-7.4.4 if02=/dev/ttyUSB2 score=17 sim_ready=True creg=True
[SELECTED] 3-7.4.4 -> /dev/ttyUSB2 sim_id=515039219149367 modem_id=866358071697796
[REGISTRY] Loaded 1 modems
```

### Common startup problems

| Log message | Cause | Fix |
|---|---|---|
| `probe failed (MODEM_OPEN_FAILED)` | Port held by another process | `fuser -v /dev/ttyUSB*` to find and kill it |
| `sim_ready=False creg=False` | No SIM inserted or not registered | Insert SIM, wait for network registration |
| `ModuleNotFoundError: No module named 'fastapi'` | Using system uvicorn instead of venv | Use `source .venv/bin/activate` first |
| All modems busy after restart | ModemManager restarted | `sudo systemctl mask ModemManager` |

---

## How Modem Discovery Works

1. Scan all `/dev/ttyUSB*` via sysfs symlinks
2. Read USB physical address and interface number from kernel path
3. Filter: vendor ID must be `2c7c` (Quectel)
4. Collect only `if=2` ports as primary (SMS interface)
5. Derive `if=3` sibling as fallback — stored, never probed at startup
6. Probe each `if=2` port: send `AT`, `AT+CPIN?`, `AT+CREG?`, `AT+CSQ`, `AT+CIMI`, `AT+CCID`, `AT+CGSN`
7. Accept modem only if: `at_ok=True` AND `sim_ready=True` AND `creg_registered=True`
8. Identity: `sim_id=IMSI`, `modem_id=IMEI`

Result: N modems = exactly N serial probes at startup.

### Registry warm refresh

After startup, the registry TTL is 10 seconds. On TTL expiry:
- If all known ports still exist on filesystem → warm refresh (update TTL only, no I/O)
- If any port disappears → full rescan

`/modems/discover` always forces a full rescan regardless of TTL.
