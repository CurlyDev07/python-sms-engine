import logging
import threading
import time

from fastapi import FastAPI

from config import settings
from modem_manager import ModemManager
from modem_registry import ModemRegistry
from schemas import HealthResponse, ModemsHealthResponse, SendRequest, SendResponse
from sms_service import SmsService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("python_sms_engine")
app = FastAPI(title="python_sms_engine")


app.state.modem_registry = ModemRegistry(
    serial_timeout=settings.serial_timeout,
    command_timeout=settings.command_timeout,
)

app.state.sms_service = SmsService(
    registry=app.state.modem_registry,
    serial_timeout=settings.serial_timeout,
    command_timeout=settings.command_timeout,
    send_timeout=settings.send_timeout,
)

app.state.modem_manager = ModemManager(
    registry=app.state.modem_registry,
)


def _auto_refresh_loop(registry: ModemRegistry, interval_seconds: float = 5.0) -> None:
    while True:
        try:
            registry.refresh()
        except Exception:
            logger.exception("AUTO_REFRESH_FAILED")
        time.sleep(interval_seconds)


@app.on_event("startup")
def startup_event() -> None:
    logger.info("STARTUP_BEGIN")

    registry: ModemRegistry = app.state.modem_registry

    try:
        registry.refresh(force=True)
        logger.info("MODEMS_LOADED_ON_STARTUP count=%s", len(registry.get_all()))
    except Exception:
        logger.exception("INITIAL_MODEM_DISCOVERY_FAILED")

    refresh_thread = threading.Thread(
        target=_auto_refresh_loop,
        args=(registry, 5.0),
        daemon=True,
        name="modem-auto-refresh",
    )
    refresh_thread.start()

    logger.info("AUTO_REFRESH_THREAD_STARTED")


@app.post("/send", response_model=SendResponse)
def send_sms(request: SendRequest) -> SendResponse:
    service: SmsService = app.state.sms_service
    return service.send(
        sim_id=request.sim_id,
        phone=request.phone,
        message=request.message,
        meta=request.meta or {},
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(success=True, service="python_sms_engine", status="ok")


@app.get("/modems/health", response_model=ModemsHealthResponse)
def modems_health() -> ModemsHealthResponse:
    manager: ModemManager = app.state.modem_manager
    return ModemsHealthResponse(success=True, modems=manager.health())


@app.get("/modems/discover")
def discover_modems() -> dict:
    try:
        registry: ModemRegistry = app.state.modem_registry
        registry.refresh(force=True)
        modems = registry.get_all()
        return {"success": True, "modems": modems}
    except Exception:
        logger.exception("MODEM_DISCOVERY_FAILED")
        return {"success": False, "error": "DISCOVERY_FAILED", "modems": []}


@app.get("/modems/summary")
def modems_summary() -> dict:
    try:
        manager: ModemManager = app.state.modem_manager
        return {"success": True, "summary": manager.summary()}
    except Exception:
        logger.exception("MODEM_SUMMARY_FAILED")
        return {"success": False, "error": "SUMMARY_FAILED", "summary": {}}


@app.get("/modems/available")
def available_modems() -> dict:
    try:
        manager: ModemManager = app.state.modem_manager
        return {"success": True, "modems": manager.get_available_modems()}
    except Exception:
        logger.exception("AVAILABLE_MODEMS_FAILED")
        return {"success": False, "error": "AVAILABLE_MODEMS_FAILED", "modems": []}


@app.get("/modems/debug")
def debug_modems() -> dict:
    try:
        manager: ModemManager = app.state.modem_manager
        return {"success": True, "modems": manager.debug_dump()}
    except Exception:
        logger.exception("MODEM_DEBUG_FAILED")
        return {"success": False, "error": "MODEM_DEBUG_FAILED", "modems": []}