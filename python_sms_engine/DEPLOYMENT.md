# Python SMS Engine — Deployment Guide

This guide covers: fresh server setup, migrating to a new server, and deploying updates.

---

## Fresh Server Setup

### 1. Prerequisites

```bash
# Python 3.10+
python3 --version

# Add user to dialout group (required for serial port access)
sudo usermod -aG dialout $USER
# Log out and back in after this
```

### 2. Disable ModemManager (required)

ModemManager auto-claims USB modems and will block all serial port access.

```bash
sudo systemctl stop ModemManager
sudo systemctl mask ModemManager

# Verify
systemctl status ModemManager
# Should show: masked
```

### 3. Clone the repo

```bash
cd ~/Documents/WebDev
git clone <repo-url> python-sms-engine
cd python-sms-engine/python_sms_engine
```

### 4. Create the Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Create the environment file

```bash
nano .env
```

Use `.env.example` as a template:
```bash
cp .env.example .env
nano .env
```

At minimum, set the token and inbound webhook URL:
```
SMS_PYTHON_API_TOKEN=your_token_here
SMS_ENGINE_INBOUND_WEBHOOK_URL=http://127.0.0.1:8081/api/gateway/inbound
```

All available variables:
```
# Auth
SMS_PYTHON_API_TOKEN=your_token_here

# Server
SMS_ENGINE_HOST=0.0.0.0
SMS_ENGINE_PORT=9000

# Serial / modem timeouts
SMS_ENGINE_SERIAL_TIMEOUT=3
SMS_ENGINE_COMMAND_TIMEOUT=10
SMS_ENGINE_SEND_TIMEOUT=30

# Inbound SMS (customer replies)
SMS_ENGINE_INBOUND_WEBHOOK_URL=http://127.0.0.1:8081/api/gateway/inbound
SMS_ENGINE_INBOUND_RETRY_MAX=10
```

Lock permissions so other users cannot read it:
```bash
chmod 600 .env
```

Make sure `.env` is in `.gitignore` so the token is never committed:
```bash
grep ".env" .gitignore || echo ".env" >> .gitignore
```

### 6. Create the systemd service

```bash
sudo nano /etc/systemd/system/sms-engine.service
```

Paste (adjust paths if your username or directory differs):
```ini
[Unit]
Description=Python SMS Engine
After=network.target

[Service]
User=reg
WorkingDirectory=/home/reg/Documents/WebDev/python-sms-engine/python_sms_engine
EnvironmentFile=/home/reg/Documents/WebDev/python-sms-engine/python_sms_engine/.env
ExecStart=/home/reg/Documents/WebDev/python-sms-engine/python_sms_engine/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 9000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

### 7. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable sms-engine
sudo systemctl start sms-engine
```

### 8. Verify it's running

```bash
sudo systemctl status sms-engine
curl -s http://127.0.0.1:9000/health
```

Expected:
```json
{"success": true, "service": "python_sms_engine", "status": "ok"}
```

---

## Migrating to a New Server

Follow the full Fresh Server Setup above, then:

1. Copy your `.env` file manually (never via git):
   ```bash
   scp oldserver:/home/reg/Documents/WebDev/python-sms-engine/python_sms_engine/.env \
       newserver:/home/reg/Documents/WebDev/python-sms-engine/python_sms_engine/.env
   ```

2. On the new server, verify modems are detected:
   ```bash
   curl -s -H "X-Gateway-Token: your_token_here" http://127.0.0.1:9000/modems/discover | python3 -m json.tool
   ```

3. Update the Laravel `.env` on the Laravel server to point to the new server IP:
   ```
   SMS_ENGINE_URL=http://new-server-ip:9000
   ```

---

## Deploying Updates (Git Pull → Live)

Use this every time you pull new code from git.

### Standard update

```bash
cd ~/Documents/WebDev/python-sms-engine/python_sms_engine

# Pull latest code
git pull

# Restart the service to load new code
sudo systemctl restart sms-engine

# Verify it came back up
sudo systemctl status sms-engine
curl -s http://127.0.0.1:9000/health
```

### If dependencies changed (requirements.txt updated)

```bash
cd ~/Documents/WebDev/python-sms-engine/python_sms_engine
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart sms-engine
curl -s http://127.0.0.1:9000/health
```

---

## Service Management Reference

```bash
# Start
sudo systemctl start sms-engine

# Stop
sudo systemctl stop sms-engine

# Restart (use after every code update)
sudo systemctl restart sms-engine

# Check status
sudo systemctl status sms-engine

# View live logs
journalctl -u sms-engine -f

# View last 100 log lines
journalctl -u sms-engine -n 100
```

---

## Verify Modems After Restart

```bash
# All detected modems (includes unhealthy)
curl -s -H "X-Gateway-Token: your_token_here" http://127.0.0.1:9000/modems/discover | python3 -m json.tool

# Only send-ready modems
curl -s -H "X-Gateway-Token: your_token_here" http://127.0.0.1:9000/modems/available | python3 -m json.tool

# Quick summary (total / online / offline)
curl -s -H "X-Gateway-Token: your_token_here" http://127.0.0.1:9000/modems/summary | python3 -m json.tool
```

A healthy modem row looks like:
```json
{
    "sim_id": "515039219149367",
    "send_ready": true,
    "identifier_source": "imsi",
    "probe_error": null,
    "at_ok": true,
    "sim_ready": true,
    "creg_registered": true
}
```

`send_ready: true` = safe to send SMS through this modem.

---

## Troubleshooting

### Service won't start

```bash
journalctl -u sms-engine -n 50
```

Common causes:
- Wrong path in `sms-engine.service` — check `WorkingDirectory` and `ExecStart`
- `.env` file missing or wrong permissions — `ls -la .env`
- Port 9000 already in use — `fuser -k 9000/tcp` then restart

### All modems show PROBE_TIMEOUT

```bash
# Check if ModemManager is running (must be masked)
systemctl status ModemManager

# Check if another process holds the ports
fuser -v /dev/ttyUSB*
```

### Modem shows at_ok=true but sim_ready=false

SIM not inserted or not seated properly. Check physically, then restart the service.

### Port not showing up in discover

```bash
# List all ttyUSB devices
ls /dev/ttyUSB*

# Check kernel USB events
dmesg | grep -E "usb|ttyUSB" | tail -20
```
