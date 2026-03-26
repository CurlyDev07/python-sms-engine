from fastapi import FastAPI
from schemas import (
    SendRequest,
    SendResponse,
    HealthResponse,
    ModemsHealthResponse,
)
from sms_service import SmsService
from modem_manager import ModemManager
from config import settings, load_sim_map_safe
from modem_detector import detect_modems

import logging

logger = logging.getLogger("python_sms_engine")

app = FastAPI()


app.state.sms_service = SmsService(
    sim_map=sim_map,
    serial_timeout=settings.serial_timeout,
    command_timeout=settings.command_timeout,
    send_timeout=settings.send_timeout,
)

app.state.modem_manager = ModemManager(
    sim_map=sim_map,
    serial_timeout=settings.serial_timeout,
    command_timeout=settings.command_timeout,
)

# -----------------------------
# SEND SMS
# -----------------------------
@app.post("/send", response_model=SendResponse)
def send_sms(request: SendRequest) -> SendResponse:
    service: SmsService = app.state.sms_service
    return service.send(
        sim_id=request.sim_id,
        phone=request.phone,
        message=request.message,
        meta=request.meta or {},
    )


# -----------------------------
# HEALTH CHECK
# -----------------------------
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        success=True,
        service="python_sms_engine",
        status="ok"
    )


# -----------------------------
# EXISTING MODEM HEALTH (STATIC MAP)
# -----------------------------
@app.get("/modems/health", response_model=ModemsHealthResponse)
def modems_health() -> ModemsHealthResponse:
    sim_map, error = load_sim_map_safe(settings.sim_map_file)
    manager: ModemManager = app.state.modem_manager

    if error:
        logger.error(
            "SIM_MAP_LOAD_FAILED file=%s error=%s",
            settings.sim_map_file,
            error
        )
    else:
        manager.update_sim_map(sim_map)

    return ModemsHealthResponse(
        success=True,
        modems=manager.health()
    )


# -----------------------------
# 🔥 NEW: AUTO MODEM DISCOVERY (SaaS LEVEL)
# -----------------------------
@app.get("/modems/discover")
def discover_modems():
    try:
        modems = detect_modems()

        return {
            "success": True,
            "modems": modems
        }

    except Exception as e:
        logger.error("MODEM_DISCOVERY_FAILED error=%s", str(e))

        return {
            "success": False,
            "error": str(e),
            "modems": []
        }