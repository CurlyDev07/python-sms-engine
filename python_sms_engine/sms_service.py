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

    def _port_for_sim(self, sim_id: str) -> str:
        modem = self.registry.get_by_sim_id(sim_id=sim_id)

        if modem and modem.get("at_ok"):
            port = modem.get("port")
            if isinstance(port, str) and port:
                return port

        raise SMSExecutionError("SIM_NOT_MAPPED")

    def _send_via_port(self, port: str, phone: str, message: str) -> Dict[str, str]:
        client = ModemATClient(
            port=port,
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
        )

        return client.send_sms(
            phone=phone,
            message=message,
            global_timeout=self.send_timeout,
        )

    def send(
        self,
        sim_id: str,  # 🔥 NOW STRING
        phone: str,
        message: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> SendResponse:

        del meta
        port: Optional[str] = None
        started_at = time.monotonic()

        try:
            # STEP 1: get primary port
            port = self._port_for_sim(sim_id)

            logger.info(
                "SMS_SEND_ATTEMPT sim_id=%s port=%s phone=%s",
                sim_id,
                port,
                phone,
            )

            # STEP 2: TRY PRIMARY
            try:
                raw_steps = self._send_via_port(port, phone, message)

                duration_ms = int((time.monotonic() - started_at) * 1000)
                merged_raw = _truncate_raw("\n".join(v for v in raw_steps.values() if v))

                logger.info(
                    "SMS_SEND_SUCCESS sim_id=%s port=%s duration_ms=%s",
                    sim_id,
                    port,
                    duration_ms,
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

            except SMSExecutionError as primary_error:
                logger.warning(
                    "PRIMARY FAILED sim_id=%s port=%s error=%s",
                    sim_id,
                    port,
                    primary_error.code,
                )

                # STEP 3: RETRY ON SAME PORT (quick retry)
                try:
                    time.sleep(0.5)
                    raw_steps = self._send_via_port(port, phone, message)

                    logger.info(
                        "RETRY SUCCESS sim_id=%s port=%s",
                        sim_id,
                        port,
                    )

                    return SendResponse(
                        success=True,
                        message_id=None,
                        error=None,
                        raw={
                            "sim_id": sim_id,
                            "port": port,
                            "status": "retry_success",
                        },
                    )

                except SMSExecutionError:
                    pass

                # STEP 4: FAILOVER (if03)
                # Use fallback_port stored in registry by the detector.
                # The old string-manipulation approach no longer works because
                # ports are now /dev/ttyUSBX paths with no "if02" in them.
                _modem = self.registry.get_by_sim_id(sim_id)
                fallback = _modem.get("fallback_port") if _modem else None

                if fallback:
                    logger.warning(
                        "FALLBACK ATTEMPT sim_id=%s fallback_port=%s",
                        sim_id,
                        fallback,
                    )

                    try:
                        raw_steps = self._send_via_port(fallback, phone, message)

                        logger.info(
                            "FALLBACK SUCCESS sim_id=%s port=%s",
                            sim_id,
                            fallback,
                        )

                        # 🔥 IMPORTANT: update registry dynamically
                        modem = self.registry.get_by_sim_id(sim_id)
                        if modem:
                            modem["port"] = fallback

                        return SendResponse(
                            success=True,
                            message_id=None,
                            error=None,
                            raw={
                                "sim_id": sim_id,
                                "port": fallback,
                                "status": "fallback_success",
                            },
                        )

                    except SMSExecutionError as fallback_error:
                        logger.error(
                            "FALLBACK FAILED sim_id=%s error=%s",
                            sim_id,
                            fallback_error.code,
                        )
                        raise fallback_error

                # No fallback or all failed
                raise primary_error

        except SMSExecutionError as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)

            logger.error(
                "SMS_SEND_FAILED sim_id=%s port=%s duration_ms=%s error=%s",
                sim_id,
                port,
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
                "SMS_SEND_FAILED sim_id=%s port=%s duration_ms=%s error=%s",
                sim_id,
                port,
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