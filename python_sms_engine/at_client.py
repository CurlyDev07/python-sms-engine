import logging
import os
import re
import threading
import time
import uuid
from typing import Dict, Iterable, Optional, Tuple

import serial


logger = logging.getLogger("python_sms_engine.at_client")

ALLOWED_ERRORS = {
    "SIM_NOT_MAPPED",
    "PORT_NOT_FOUND",
    "MODEM_OPEN_FAILED",
    "MODEM_TIMEOUT",
    "AT_NOT_RESPONDING",
    "CMGF_FAILED",
    "CMGS_PROMPT_FAILED",
    "SEND_FAILED",
    "UNKNOWN_ERROR",
}

# ---------------------------------------------------------------------------
# Per-port send locks
#
# Only one writer may hold a port's lock at a time. send_sms() acquires the
# lock for its entire transaction (open → close). InboundListener._cmd()
# acquires it briefly around each AT write.
#
# This prevents AT command bytes from a concurrent writer (inbound listener
# session restarts, discovery probes) from landing inside the AT+CMGS
# text-entry window and corrupting the outbound SMS body.
# ---------------------------------------------------------------------------
_port_locks: Dict[str, threading.Lock] = {}
_port_locks_guard = threading.Lock()


def get_port_lock(port: str) -> threading.Lock:
    """Return the shared per-port write lock for the given serial port path."""
    with _port_locks_guard:
        if port not in _port_locks:
            _port_locks[port] = threading.Lock()
        return _port_locks[port]


def _parse_at_error_codes(raw: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Extract numeric codes from +CMS ERROR and +CME ERROR responses.

    +CMS ERROR = network/carrier layer (e.g. 50 = no credit, 38 = network down)
    +CME ERROR = modem/equipment layer (e.g. 10 = SIM not inserted, 13 = SIM failure)

    Returns (cms_code, cme_code). Either or both may be None.
    """
    cms_code: Optional[int] = None
    cme_code: Optional[int] = None
    for line in raw.splitlines():
        m = re.search(r"\+CMS ERROR:\s*(\d+)", line)
        if m:
            cms_code = int(m.group(1))
        m = re.search(r"\+CME ERROR:\s*(\d+)", line)
        if m:
            cme_code = int(m.group(1))
    return cms_code, cme_code


class SMSExecutionError(Exception):
    def __init__(
        self,
        code: str,
        raw: Optional[str] = None,
        cms_code: Optional[int] = None,
        cme_code: Optional[int] = None,
    ) -> None:
        if code not in ALLOWED_ERRORS:
            code = "UNKNOWN_ERROR"
        self.code = code
        self.raw = raw
        self.cms_code = cms_code   # network/carrier error code
        self.cme_code = cme_code   # modem/equipment error code
        super().__init__(code)


class ModemATClient:
    def __init__(
        self,
        port: str,
        serial_timeout: float,
        command_timeout: float,
        baudrate: int = 115200,
    ) -> None:
        self.port = port
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout
        self.baudrate = baudrate
        self._serial: Optional[serial.Serial] = None

    def open(self) -> None:
        if not os.path.exists(self.port):
            raise SMSExecutionError("PORT_NOT_FOUND")

        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=self.serial_timeout,
                write_timeout=self.serial_timeout,
            )
            time.sleep(0.5)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except FileNotFoundError as exc:
            raise SMSExecutionError("PORT_NOT_FOUND") from exc
        except serial.SerialException as exc:
            text = str(exc).lower()
            if "no such file" in text or "file not found" in text:
                raise SMSExecutionError("PORT_NOT_FOUND") from exc
            raise SMSExecutionError("MODEM_OPEN_FAILED") from exc
        except Exception as exc:
            raise SMSExecutionError("MODEM_OPEN_FAILED") from exc

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass

    def _ensure_open(self) -> serial.Serial:
        if self._serial is None or not self._serial.is_open:
            raise SMSExecutionError("MODEM_OPEN_FAILED")
        return self._serial

    def _step_timeout(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SMSExecutionError("MODEM_TIMEOUT")
        return min(self.command_timeout, remaining)

    def _read_until(
        self,
        expected: Iterable[str],
        failure: Iterable[str],
        timeout: float,
        timeout_code: str,
    ) -> str:
        ser = self._ensure_open()
        deadline = time.monotonic() + timeout
        buffer = ""

        while time.monotonic() < deadline:
            try:
                chunk = ser.read(256)
            except serial.SerialTimeoutException as exc:
                raise SMSExecutionError(timeout_code, raw=buffer) from exc
            except Exception as exc:
                raise SMSExecutionError("UNKNOWN_ERROR", raw=buffer) from exc

            if not chunk:
                continue

            decoded = chunk.decode("utf-8", errors="ignore")
            buffer += decoded

            if any(token in buffer for token in failure):
                return buffer
            if any(token in buffer for token in expected):
                return buffer

        raise SMSExecutionError(timeout_code, raw=buffer)

    def _write(self, data: bytes, timeout_code: str, raw: Optional[str] = None) -> None:
        ser = self._ensure_open()
        try:
            ser.write(data)
            ser.flush()
        except serial.SerialTimeoutException as exc:
            raise SMSExecutionError(timeout_code, raw=raw) from exc
        except Exception as exc:
            raise SMSExecutionError("UNKNOWN_ERROR", raw=raw) from exc

    def _command_expect_ok(
        self,
        command: str,
        fail_code: str,
        deadline: float,
        retries: int = 0,
    ) -> str:
        last_response = ""
        attempts = retries + 1

        for _ in range(attempts):
            timeout = self._step_timeout(deadline)
            self._write(f"{command}\r".encode("utf-8"), timeout_code=fail_code, raw=last_response)
            response = self._read_until(
                expected=["OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=timeout,
                timeout_code=fail_code,
            )
            last_response = response
            if "OK" in response and "ERROR" not in response:
                return response

        raise SMSExecutionError(fail_code, raw=last_response)

    def _parse_final_response(self, response: str) -> bool:
        if "+CMS ERROR" in response or "+CME ERROR" in response or "ERROR" in response:
            cms_code, cme_code = _parse_at_error_codes(response)
            raise SMSExecutionError(
                "SEND_FAILED",
                raw=response,
                cms_code=cms_code,
                cme_code=cme_code,
            )
        if "+CMGS:" in response or "OK" in response:
            return True
        raise SMSExecutionError("UNKNOWN_ERROR", raw=response)

    def check_at(self, timeout: Optional[float] = None) -> bool:
        global_timeout = timeout if timeout is not None else self.command_timeout
        deadline = time.monotonic() + global_timeout
        opened = False

        try:
            self.open()
            opened = True
            self._command_expect_ok("AT", "AT_NOT_RESPONDING", deadline=deadline, retries=0)
            return True
        except SMSExecutionError:
            return False
        finally:
            if opened:
                self.close()

    def probe(self, timeout: Optional[float] = None) -> Dict[str, bool]:
        global_timeout = timeout if timeout is not None else self.command_timeout
        deadline = time.monotonic() + global_timeout
        opened = False
        reachable = False
        at_ok = False

        try:
            self.open()
            opened = True
            reachable = True
            self._command_expect_ok("AT", "AT_NOT_RESPONDING", deadline=deadline, retries=0)
            at_ok = True
        except SMSExecutionError:
            at_ok = False
        finally:
            if opened:
                self.close()

        return {"reachable": reachable, "at_ok": at_ok}

    def send_sms(self, phone: str, message: str, global_timeout: float) -> Dict[str, str]:
        tx_id = uuid.uuid4().hex[:8]
        deadline = time.monotonic() + global_timeout

        responses: Dict[str, str] = {
            "at": "",
            "ate0": "",
            "cmgf": "",
            "cmgs_prompt": "",
            "final": "",
        }

        opened = False
        lock_held = False
        port_lock = get_port_lock(self.port)

        try:
            # Acquire per-port lock before opening.
            # Held for the entire transaction so no concurrent writer
            # (inbound listener, discovery probe) can inject bytes into
            # the CMGS text-entry window.
            lock_timeout = max(1.0, deadline - time.monotonic())
            if not port_lock.acquire(timeout=lock_timeout):
                raise SMSExecutionError("MODEM_TIMEOUT")
            lock_held = True

            logger.info("SEND_TX_BEGIN tx_id=%s port=%s", tx_id, self.port)

            self.open()
            opened = True

            if self._serial:
                self._serial.write(b"\r\r\r")
                time.sleep(0.2)
                self._serial.reset_input_buffer()

            # AT CHECK
            for _ in range(3):
                try:
                    responses["at"] = self._command_expect_ok(
                        "AT",
                        "AT_NOT_RESPONDING",
                        deadline=deadline,
                        retries=0,
                    )
                    if "OK" in responses["at"]:
                        break
                except Exception:
                    time.sleep(0.3)
            else:
                raise SMSExecutionError("AT_NOT_RESPONDING")

            time.sleep(0.1)

            # RESET MODEM STATE
            try:
                self._command_expect_ok("ATZ", "AT_NOT_RESPONDING", deadline=deadline, retries=0)
            except SMSExecutionError:
                pass
            time.sleep(0.3)

            # DISABLE ECHO
            responses["ate0"] = self._command_expect_ok(
                "ATE0",
                "AT_NOT_RESPONDING",
                deadline=deadline,
                retries=0,
            )
            time.sleep(0.1)

            # TEXT MODE
            responses["cmgf"] = self._command_expect_ok(
                "AT+CMGF=1",
                "CMGF_FAILED",
                deadline=deadline,
                retries=1,
            )
            time.sleep(0.1)

            # Extra flush immediately before CMGS — clears any bytes that
            # accumulated in the kernel tty buffer during setup (echo fragments,
            # unsolicited +CMT lines from inbound listener's fd).
            if self._serial:
                self._serial.reset_input_buffer()

            # START SEND
            self._write(
                f'AT+CMGS="{phone}"\r'.encode("utf-8"),
                timeout_code="CMGS_PROMPT_FAILED",
                raw=responses["cmgf"],
            )

            responses["cmgs_prompt"] = self._read_until(
                expected=[">"],
                failure=["ERROR", "+CMS ERROR", "+CME ERROR"],
                timeout=self._step_timeout(deadline),
                timeout_code="CMGS_PROMPT_FAILED",
            )

            if ">" not in responses["cmgs_prompt"]:
                raise SMSExecutionError("CMGS_PROMPT_FAILED", raw=responses["cmgs_prompt"])

            logger.info("SEND_PROMPT_READY tx_id=%s port=%s", tx_id, self.port)

            # WRITE MESSAGE BODY — lock still held; no concurrent writer can
            # inject bytes between here and the 0x1A terminator.
            payload = message.encode("utf-8", errors="ignore") + bytes([26])
            self._write(payload, timeout_code="SEND_FAILED", raw=responses["cmgs_prompt"])

            logger.info(
                "SEND_BODY_WRITE tx_id=%s port=%s message_len=%s",
                tx_id, self.port, len(message),
            )

            # WAIT FOR NETWORK
            time.sleep(1.5)

            final_buffer = ""
            start_time = time.monotonic()

            while True:
                if time.monotonic() - start_time > max(5, self._step_timeout(deadline)):
                    break

                if self._serial:
                    try:
                        chunk = self._serial.read_all().decode("utf-8", errors="ignore")
                        if chunk:
                            final_buffer += chunk
                    except Exception:
                        pass

                time.sleep(0.2)

            responses["final"] = final_buffer.strip()

            self._parse_final_response(responses["final"])

            logger.info("SEND_TX_END tx_id=%s port=%s status=success", tx_id, self.port)

            return responses

        except SMSExecutionError as exc:
            logger.warning(
                "SEND_TX_END tx_id=%s port=%s status=failed error=%s",
                tx_id, self.port, exc.code,
            )

            raw_parts = [
                responses.get("at", ""),
                responses.get("ate0", ""),
                responses.get("cmgf", ""),
                responses.get("cmgs_prompt", ""),
                responses.get("final", ""),
                exc.raw or "",
            ]

            merged_raw = "\n".join(part for part in raw_parts if part)

            raise SMSExecutionError(exc.code, raw=merged_raw or None, cms_code=exc.cms_code, cme_code=exc.cme_code) from exc

        finally:
            if opened:
                self.close()
            if lock_held:
                port_lock.release()
