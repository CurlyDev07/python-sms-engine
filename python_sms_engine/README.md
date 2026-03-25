# Python SMS Engine (Execution Plane)

This service is the **execution layer** for SMS delivery.

- Laravel SMS Gateway = **control plane**
- Python SMS Engine = **execution plane**

Python handles only:
- modem communication
- ttyUSB mapping
- AT commands
- SMS execution
- modem health checks

Python does **not** handle queueing, retry policy, AI logic, business logic, or database storage.

## Project Structure

- `app.py` - FastAPI app and endpoints
- `config.py` - environment config and SIM map loading
- `schemas.py` - request/response schemas
- `at_client.py` - low-level serial + AT command client
- `sms_service.py` - SMS send orchestration and normalized responses
- `modem_manager.py` - modem health checks
- `sim_map.example.json` - sample SIM-to-port mapping

## Requirements

- Python 3.10+
- USB modem(s) exposed as `/dev/ttyUSB*`

## Setup

```bash
cd python_sms_engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp sim_map.example.json sim_map.json
```

## Configuration

Environment variables:

- `SMS_ENGINE_SIM_MAP_FILE` (default: `sim_map.json`)
- `SMS_ENGINE_SERIAL_TIMEOUT` (default: `3`)
- `SMS_ENGINE_COMMAND_TIMEOUT` (default: `10`)
- `SMS_ENGINE_HOST` (default: `0.0.0.0`)
- `SMS_ENGINE_PORT` (default: `8000`)

## Run

```bash
cd python_sms_engine
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Test Endpoints

### Health

```bash
curl http://127.0.0.1:8000/health
```

### Modem Health

```bash
curl http://127.0.0.1:8000/modems/health
```

### Send SMS

```bash
curl -X POST http://127.0.0.1:8000/send \
  -H 'Content-Type: application/json' \
  -d '{
    "sim_id": 12,
    "phone": "09171234567",
    "message": "Hello world",
    "meta": {
      "message_id": 999,
      "company_id": 1,
      "message_type": "CHAT"
    }
  }'
```

## ttyUSB Notes

- Keep SIM mapping in `sim_map.json`; no hardcoded modem ports.
- Ensure process user has permission to access `/dev/ttyUSB*`.
- Use stable udev rules in production if modem indexes can change.

## API Contracts

### `POST /send`

Success response:

```json
{
  "success": true,
  "message_id": null,
  "error": null,
  "raw": {
    "sim_id": 12,
    "port": "/dev/ttyUSB2",
    "status": "success"
  }
}
```

Failure response:

```json
{
  "success": false,
  "message_id": null,
  "error": "MODEM_TIMEOUT",
  "raw": {
    "sim_id": 12,
    "port": "/dev/ttyUSB2"
  }
}
```

Normalized error set:

- `SIM_NOT_MAPPED`
- `PORT_NOT_FOUND`
- `MODEM_OPEN_FAILED`
- `MODEM_TIMEOUT`
- `AT_NOT_RESPONDING`
- `CMGF_FAILED`
- `CMGS_PROMPT_FAILED`
- `SEND_FAILED`
- `UNKNOWN_ERROR`
