import logging
import time
from typing import Any, Dict, Optional

from at_client import ModemATClient, SMSExecutionError
from modem_registry import ModemRegistry
from schemas import SendResponse

logger = logging.getLogger("python_sms_engine")

RAW_MAX_LEN = 500

# Error layer classification for Laravel to distinguish failure cause:
#   hardware → serial/port-level failure (modem physically dead or unplugged)
#   modem    → CME error (SIM not inserted, SIM failure, PIN required, etc.)
#   network  → CMS error (no credit, destination unreachable, carrier reject)
_HARDWARE_ERRORS = {
    "PORT_NOT_FOUND",
    "MODEM_OPEN_FAILED",
    "MODEM_TIMEOUT",
    "AT_NOT_RESPONDING",
    "SIM_NOT_MAPPED",
}


def _error_layer(exc: SMSExecutionError) -> str:
    if exc.code in _HARDWARE_ERRORS:
        return "hardware"
    if exc.cme_code is not None:
        return "modem"
    if exc.cms_code is not None:
        return "network"
    return "unknown"


def _truncate_raw(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if len(text) <= RAW_MAX_LEN:
        return text
    return f"{text[:RAW_MAX_LEN]}...<truncated>"


class SmsService:
    def __init__(
        self,
        registry: ModemRegistry,
        serial_timeout: float,
        command_timeout: float,
        send_timeout: float,
    ) -> None:
        self.registry = registry
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout
        self.send_timeout = send_timeout

    def _port_for_sim(self, sim_id: str) -> str:
        modem = self.registry.get_by_sim_id(sim_id=sim_id)

        if modem and modem.get("at_ok"):
            port = modem.get("port")
            if isinstance(port, str) and port:
                return port

        raise SMSExecutionError("SIM_NOT_MAPPED")

    def _send_via_port(self, port: str, phone: str, message: str, sim_id: Optional[str] = None) -> Dict[str, str]:
        client = ModemATClient(
            port=port,
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
        )
        return client.send_sms(
            phone=phone,
            message=message,
            global_timeout=self.send_timeout,
            sim_id=sim_id,
        )

    def send(
        self,
        sim_id: str,
        phone: str,
        message: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> SendResponse:

        meta = meta or {}
        message_id = str(meta["message_id"]) if meta.get("message_id") is not None else None
        port: Optional[str] = None
        started_at = time.monotonic()

        try:
            # STEP 1: resolve port
            port = self._port_for_sim(sim_id)
            modem = self.registry.get_by_sim_id(sim_id)
            modem_id = modem.get("modem_id") if modem else None

            logger.info(
                "SMS_SEND_ATTEMPT sim_id=%s modem_id=%s port=%s phone=%s",
                sim_id, modem_id, port, phone,
            )

            # STEP 2: try primary port (if02)
            try:
                raw_steps = self._send_via_port(port, phone, message, sim_id=sim_id)
                duration_ms = int((time.monotonic() - started_at) * 1000)
                merged_raw = _truncate_raw("\n".join(v for v in raw_steps.values() if v))

                logger.info(
                    "SMS_SEND_SUCCESS sim_id=%s modem_id=%s port=%s duration_ms=%s",
                    sim_id, modem_id, port, duration_ms,
                )

                return SendResponse(
                    success=True,
                    message_id=message_id,
                    error=None,
                    raw={
                        "sim_id": sim_id,
                        "modem_id": modem_id,
                        "port": port,
                        "status": "success",
                        "modem_response": merged_raw,
                        "meta": meta,
                    },
                )

            except SMSExecutionError as primary_error:
                logger.warning(
                    "PRIMARY FAILED sim_id=%s modem_id=%s port=%s error=%s",
                    sim_id, modem_id, port, primary_error.code,
                )

                # Network/modem errors (CMS/CME) won't be fixed by retrying a
                # different port — fail immediately so Laravel gets the error fast.
                if primary_error.cms_code is not None or primary_error.cme_code is not None:
                    raise primary_error

                # STEP 3: retry on same port (hardware errors only)
                try:
                    time.sleep(0.5)
                    raw_steps = self._send_via_port(port, phone, message, sim_id=sim_id)
                    logger.info("RETRY SUCCESS sim_id=%s port=%s", sim_id, port)

                    return SendResponse(
                        success=True,
                        message_id=message_id,
                        error=None,
                        raw={
                            "sim_id": sim_id,
                            "modem_id": modem_id,
                            "port": port,
                            "status": "retry_success",
                            "meta": meta,
                        },
                    )

                except SMSExecutionError:
                    pass

                # STEP 4: failover to if03 (hardware errors only)
                _modem = self.registry.get_by_sim_id(sim_id)
                fallback = _modem.get("fallback_port") if _modem else None

                if fallback:
                    logger.warning(
                        "FALLBACK ATTEMPT sim_id=%s fallback_port=%s",
                        sim_id, fallback,
                    )

                    try:
                        raw_steps = self._send_via_port(fallback, phone, message, sim_id=sim_id)
                        logger.info("FALLBACK SUCCESS sim_id=%s port=%s", sim_id, fallback)

                        # update registry so next send uses if03 directly
                        if _modem:
                            _modem["port"] = fallback

                        return SendResponse(
                            success=True,
                            message_id=message_id,
                            error=None,
                            raw={
                                "sim_id": sim_id,
                                "modem_id": modem_id,
                                "port": fallback,
                                "status": "fallback_success",
                                "meta": meta,
                            },
                        )

                    except SMSExecutionError as fallback_error:
                        logger.error(
                            "FALLBACK FAILED sim_id=%s error=%s cms=%s cme=%s",
                            sim_id, fallback_error.code,
                            fallback_error.cms_code, fallback_error.cme_code,
                        )
                        raise fallback_error

                raise primary_error

        except SMSExecutionError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            modem = self.registry.get_by_sim_id(sim_id)
            modem_id = modem.get("modem_id") if modem else None

            logger.error(
                "SMS_SEND_FAILED sim_id=%s modem_id=%s port=%s duration_ms=%s "
                "error=%s error_layer=%s cms=%s cme=%s",
                sim_id, modem_id, port, duration_ms,
                exc.code, _error_layer(exc), exc.cms_code, exc.cme_code,
            )

            return SendResponse(
                success=False,
                message_id=message_id,
                error=exc.code,
                raw={
                    "sim_id": sim_id,
                    "modem_id": modem_id,
                    "port": port,
                    "error_layer": _error_layer(exc),
                    "cms_error_code": exc.cms_code,
                    "cme_error_code": exc.cme_code,
                    "modem_response": _truncate_raw(exc.raw),
                    "meta": meta,
                },
            )

        except Exception:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            logger.exception(
                "SMS_SEND_FAILED sim_id=%s port=%s duration_ms=%s error=UNKNOWN_ERROR",
                sim_id, port, duration_ms,
            )

            return SendResponse(
                success=False,
                message_id=message_id,
                error="UNKNOWN_ERROR",
                raw={
                    "sim_id": sim_id,
                    "modem_id": None,
                    "port": port,
                    "error_layer": "unknown",
                    "cms_error_code": None,
                    "cme_error_code": None,
                    "modem_response": None,
                    "meta": meta,
                },
            )
