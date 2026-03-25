import logging

from fastapi import FastAPI

from config import Settings, load_sim_map_safe
from modem_manager import ModemManager
from schemas import HealthResponse, ModemsHealthResponse, SendRequest, SmsSendResult
from sms_service import SMSService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("python_sms_engine")
app = FastAPI(title="python_sms_engine")
settings = Settings()


@app.on_event("startup")
def startup() -> None:
    sim_map, error = load_sim_map_safe(settings.sim_map_file)
    if error:
        logger.error("SIM_MAP_LOAD_FAILED file=%s error=%s", settings.sim_map_file, error)

    app.state.sms_service = SMSService(
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


@app.post("/send", response_model=SmsSendResult)
def send_sms(payload: SendRequest) -> SmsSendResult:
    sim_map, error = load_sim_map_safe(settings.sim_map_file)
    service: SMSService = app.state.sms_service
    manager: ModemManager = app.state.modem_manager

    if error:
        logger.error("SIM_MAP_LOAD_FAILED file=%s error=%s", settings.sim_map_file, error)
    else:
        service.update_sim_map(sim_map)
        manager.update_sim_map(sim_map)

    return service.send(
        sim_id=payload.sim_id,
        phone=payload.phone,
        message=payload.message,
        meta=payload.meta,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(success=True, service="python_sms_engine", status="ok")


@app.get("/modems/health", response_model=ModemsHealthResponse)
def modems_health() -> ModemsHealthResponse:
    sim_map, error = load_sim_map_safe(settings.sim_map_file)
    manager: ModemManager = app.state.modem_manager

    if error:
        logger.error("SIM_MAP_LOAD_FAILED file=%s error=%s", settings.sim_map_file, error)
    else:
        manager.update_sim_map(sim_map)

    return ModemsHealthResponse(success=True, modems=manager.health())
