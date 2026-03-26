import logging
import time
from typing import Any, Dict, Optional

from at_client import ModemATClient, SMSExecutionError
from modem_registry import ModemRegistry
from schemas import SendResponse

logger = logging.getLogger("python_sms_engine")

RAW_MAX_LEN = 500


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

    def _port_for_sim(self, sim_id: int) -> str:
        modem = self.registry.get_by_sim_id(sim_id=sim_id)
        if modem and modem.get("at_ok"):
            port = modem.get("port")
            if isinstance(port, str) and port:
                return port

        raise SMSExecutionError("SIM_NOT_MAPPED")

    def send(
        self,
        sim_id: int,
        phone: str,
        message: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> SendResponse:

        del meta
        port: Optional[str] = None
        started_at = time.monotonic()

        try:
            port = self._port_for_sim(sim_id)

            logger.info(
                "SMS_SEND_ATTEMPT sim_id=%s port=%s phone=%s duration_ms=%s error=%s",
                sim_id,
                port,
                phone,
                0,
                None,
            )

            client = ModemATClient(
                port=port,
                serial_timeout=self.serial_timeout,
                command_timeout=self.command_timeout,
            )

            raw_steps = client.send_sms(
                phone=phone,
                message=message,
                global_timeout=self.send_timeout,
            )

            duration_ms = int((time.monotonic() - started_at) * 1000)
            merged_raw = _truncate_raw("\n".join(v for v in raw_steps.values() if v))

            logger.info(
                "SMS_SEND_SUCCESS sim_id=%s port=%s phone=%s duration_ms=%s error=%s",
                sim_id,
                port,
                phone,
                duration_ms,
                None,
            )

            return SendResponse(
                success=True,
                message_id=None,
                error=None,
                raw={
                    "sim_id": sim_id,
                    "port": port,
                    "status": "success",
                    "modem_response": merged_raw,
                },
            )

        except SMSExecutionError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)

            logger.error(
                "SMS_SEND_FAILED sim_id=%s port=%s phone=%s duration_ms=%s error=%s",
                sim_id,
                port,
                phone,
                duration_ms,
                exc.code,
            )

            return SendResponse(
                success=False,
                message_id=None,
                error=exc.code,
                raw={
                    "sim_id": sim_id,
                    "port": port,
                    "modem_response": _truncate_raw(exc.raw),
                },
            )

        except Exception:
            duration_ms = int((time.monotonic() - started_at) * 1000)

            logger.exception(
                "SMS_SEND_FAILED sim_id=%s port=%s phone=%s duration_ms=%s error=%s",
                sim_id,
                port,
                phone,
                duration_ms,
                "UNKNOWN_ERROR",
            )

            return SendResponse(
                success=False,
                message_id=None,
                error="UNKNOWN_ERROR",
                raw={
                    "sim_id": sim_id,
                    "port": port,
                    "modem_response": None,
                },
            )
