import logging
import threading
import time
from typing import Any, Dict, List, Optional

from at_client import ModemATClient, SMSExecutionError
from modem_registry import ModemRegistry
from schemas import SendResponse

logger = logging.getLogger("python_sms_engine")

RAW_MAX_LEN = 500

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
        self._clients: Dict[str, ModemATClient] = {}
        self._clients_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Persistent client management
    # ------------------------------------------------------------------

    def _make_client(self, port: str) -> ModemATClient:
        return ModemATClient(
            port=port,
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
        )

    def _get_client(self, port: str) -> ModemATClient:
        """Return the persistent client for this port, initializing if needed."""
        with self._clients_lock:
            client = self._clients.get(port)
        if client is not None:
            return client

        client = self._make_client(port)
        client.initialize(global_timeout=20.0)

        with self._clients_lock:
            if port not in self._clients:
                self._clients[port] = client
            else:
                client.close()
                client = self._clients[port]

        return client

    def warm_up(self, modems: List[Dict]) -> None:
        """Pre-open and configure persistent connections for all send ports at startup."""
        for modem in modems:
            port = modem.get("port")
            if not port:
                continue
            try:
                self._get_client(port)
                logger.info("MODEM_CLIENT_READY port=%s", port)
            except Exception as exc:
                logger.warning("MODEM_CLIENT_INIT_FAILED port=%s error=%s", port, exc)

    def close_all_clients(self) -> None:
        """Close all persistent connections so modem probes get exclusive port access."""
        with self._clients_lock:
            for port, client in list(self._clients.items()):
                try:
                    client._initialized = False
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
        logger.info("MODEM_CLIENTS_CLOSED_FOR_PROBE")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def _port_for_sim(self, sim_id: str) -> str:
        modem = self.registry.get_by_sim_id(sim_id=sim_id)
        if modem and modem.get("at_ok"):
            port = modem.get("port")
            if isinstance(port, str) and port:
                return port
        raise SMSExecutionError("SIM_NOT_MAPPED")

    def _send_via_port(self, port: str, phone: str, message: str, sim_id: Optional[str] = None) -> Dict[str, str]:
        client = self._get_client(port)
        return client.send_persistent(
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
            port = self._port_for_sim(sim_id)
            modem = self.registry.get_by_sim_id(sim_id)
            modem_id = modem.get("modem_id") if modem else None

            logger.info(
                "SMS_SEND_ATTEMPT sim_id=%s modem_id=%s port=%s phone=%s",
                sim_id, modem_id, port, phone,
            )

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

                # Network/modem errors won't be fixed by retrying — fail fast.
                if primary_error.cms_code is not None or primary_error.cme_code is not None:
                    raise primary_error

                # Hardware error — send_persistent already tried reinit+retry internally.
                # Give it one final attempt after a short pause.
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
