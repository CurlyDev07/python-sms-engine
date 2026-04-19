import logging
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException

from config import settings
from inbound_listener import InboundListener
from inbound_spool import InboundSpool
from inbound_webhook import InboundRetryWorker
from modem_manager import ModemManager
from modem_registry import ModemRegistry
from modem_watchdog import ModemWatchdog
from schemas import HealthResponse, ModemsDiscoverResponse, ModemsHealthResponse, SendRequest, SendResponse
from sms_service import SmsService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("python_sms_engine")
app = FastAPI(title="python_sms_engine")


def _require_token(x_gateway_token: str = Header(default="")) -> None:
    """
    Shared-secret auth dependency for Laravel-facing endpoints.
    Header: X-Gateway-Token
    Env:    SMS_PYTHON_API_TOKEN
    If env var is empty, auth is disabled (safe for local dev).
    """
    expected = settings.engine_token
    if not expected:
        return  # auth disabled — no token configured
    if x_gateway_token != expected:
        logger.warning("AUTH_FAILED token_provided=%s", bool(x_gateway_token))
        raise HTTPException(
            status_code=401,
            detail={"success": False, "error": "UNAUTHORIZED"},
        )


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

    # refresh_thread = threading.Thread(
    #     target=_auto_refresh_loop,
    #     args=(registry, 5.0),
    #     daemon=True,
    #     name="modem-auto-refresh",
    # )
    # refresh_thread.start()

    # logger.info("AUTO_REFRESH_THREAD_STARTED")

    # ------------------------------------------------------------------
    # Inbound SMS listener — one thread per send-ready modem
    # ------------------------------------------------------------------
    spool = InboundSpool()
    app.state.inbound_spool = spool

    retry_worker = InboundRetryWorker(
        spool=spool,
        webhook_url=settings.inbound_webhook_url,
        max_attempts=settings.inbound_retry_max,
    )
    retry_worker.start()
    app.state.inbound_retry_worker = retry_worker

    listeners = []
    for modem in registry.get_all():
        port = modem.get("port")
        sim_id = modem.get("sim_id")
        if not port or not sim_id:
            continue
        # if03 is dedicated to inbound listening; if02 stays exclusive for outbound sends.
        # Falls back to if02 if fallback_port is not present.
        listen_port = modem.get("fallback_port") or port
        listener = InboundListener(
            port=listen_port,
            runtime_sim_id=sim_id,
            spool=spool,
            webhook_url=settings.inbound_webhook_url,
            max_webhook_attempts=settings.inbound_retry_max,
        )
        listener.start()
        listeners.append(listener)
        logger.info("INBOUND_LISTENER_LAUNCHED listen_port=%s send_port=%s sim=%s", listen_port, port, sim_id)

    app.state.inbound_listeners = listeners
    logger.info("INBOUND_LISTENERS_STARTED count=%s", len(listeners))

    # Pre-open and configure persistent send connections (if02) for all ready modems.
    # After this, every send goes straight to AT+CMGS — no open/setup overhead.
    service: SmsService = app.state.sms_service
    service.warm_up(registry.get_all())
    logger.info("MODEM_CLIENTS_WARMED_UP")

    watchdog = ModemWatchdog(service=service, registry=registry, interval=30.0)
    watchdog.start()
    app.state.modem_watchdog = watchdog
    logger.info("MODEM_WATCHDOG_STARTED")


@app.post("/send", response_model=SendResponse, dependencies=[Depends(_require_token)])
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


@app.get("/modems/health", response_model=ModemsHealthResponse, dependencies=[Depends(_require_token)])
def modems_health() -> ModemsHealthResponse:
    manager: ModemManager = app.state.modem_manager
    return ModemsHealthResponse(success=True, modems=manager.health())


@app.get("/modems/discover", response_model=ModemsDiscoverResponse, dependencies=[Depends(_require_token)])
def discover_modems() -> ModemsDiscoverResponse:
    """
    Forces a full hardware rescan. Closes persistent connections first so probes
    get exclusive port access — prevents AT command injection into active sends.
    Reopens persistent connections after probing completes.
    Set DISCOVER_ENABLED=false in .env to disable without touching code.
    """
    import os
    if os.environ.get("DISCOVER_ENABLED", "true").lower() in ("false", "0", "no"):
        logger.info("DISCOVER_DISABLED — returning cached registry state")
        registry: ModemRegistry = app.state.modem_registry
        return ModemsDiscoverResponse(success=True, modems=list(registry._cache.values()))

    registry: ModemRegistry = app.state.modem_registry
    service: SmsService = app.state.sms_service
    try:
        service.close_all_clients()
        logger.info("DISCOVER_CLIENTS_CLOSED")
        modems = registry.discover()
        return ModemsDiscoverResponse(success=True, modems=modems)
    except Exception:
        logger.exception("MODEM_DISCOVERY_FAILED")
        return ModemsDiscoverResponse(success=False, modems=[])
    finally:
        service.warm_up(registry.get_all())
        logger.info("DISCOVER_CLIENTS_REOPENED")


@app.get("/modems/summary", dependencies=[Depends(_require_token)])
def modems_summary() -> dict:
    try:
        manager: ModemManager = app.state.modem_manager
        return {"success": True, "summary": manager.summary()}
    except Exception:
        logger.exception("MODEM_SUMMARY_FAILED")
        return {"success": False, "error": "SUMMARY_FAILED", "summary": {}}


@app.get("/modems/available", dependencies=[Depends(_require_token)])
def available_modems() -> dict:
    try:
        manager: ModemManager = app.state.modem_manager
        return {"success": True, "modems": manager.get_available_modems()}
    except Exception:
        logger.exception("AVAILABLE_MODEMS_FAILED")
        return {"success": False, "error": "AVAILABLE_MODEMS_FAILED", "modems": []}


@app.get("/modems/debug", dependencies=[Depends(_require_token)])
def debug_modems() -> dict:
    try:
        manager: ModemManager = app.state.modem_manager
        return {"success": True, "modems": manager.debug_dump()}
    except Exception:
        logger.exception("MODEM_DEBUG_FAILED")
        return {"success": False, "error": "MODEM_DEBUG_FAILED", "modems": []}


# ---------------------------------------------------------------------------
# DEV STUB — Step 9 terminal network-layer failure proof
# Remove this endpoint before production deployment.
# ---------------------------------------------------------------------------
@app.post("/dev/stub/send-network-fail", response_model=SendResponse, dependencies=[Depends(_require_token)])
def dev_stub_network_fail(request: SendRequest) -> SendResponse:
    """
    Returns a deterministic terminal network-layer failure response.
    Does not touch any modem hardware.
    Use only to prove Step 9: Laravel marks message failed, scheduled_at=null,
    retry scheduler does not re-queue it.
    """
    meta = request.meta or {}
    message_id = str(meta["message_id"]) if meta.get("message_id") is not None else None

    logger.info(
        "DEV_STUB_NETWORK_FAIL sim_id=%s message_id=%s",
        request.sim_id, message_id,
    )

    return SendResponse(
        success=False,
        message_id=message_id,
        error="SEND_FAILED",
        raw={
            "sim_id": request.sim_id,
            "modem_id": "STUB_MODEM_ID",
            "port": "/dev/ttyUSB_STUB",
            "error_layer": "network",
            "cms_error_code": 27,
            "cme_error_code": None,
            "modem_response": "+CMS ERROR: 27",
            "meta": meta,
        },
    )