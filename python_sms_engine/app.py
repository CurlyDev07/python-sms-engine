import logging

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
        registry.refresh()
        modems = registry.get_all()
        return {"success": True, "modems": modems}
    except Exception:
        logger.exception("MODEM_DISCOVERY_FAILED")
        return {"success": False, "error": "DISCOVERY_FAILED", "modems": []}
