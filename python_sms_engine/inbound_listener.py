"""
Inbound SMS listener — one thread per active modem port.

Startup sequence per modem:
  1. Open serial port (blocking reads — no timeout on the outer loop)
  2. AT+CMGF=1         — text mode
  3. AT+CNMI=2,2,0,0,0 — push unsolicited +CMT to serial immediately on receipt
  4. AT+CMGL="ALL"     — drain messages that arrived before listener started
  5. Enter blocking readline loop — waits for +CMT unsolicited notifications

+CMT format (two lines):
  +CMT: "+639171234567","","26/04/13,21:00:00+32"
  Hello this is a reply

Flow per received message:
  parse → spool.insert() → AT+CMGD (delete from SIM) → deliver_one() to Laravel
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import serial

from inbound_spool import InboundSpool
from inbound_webhook import deliver_one

logger = logging.getLogger("python_sms_engine.inbound_listener")

# Regex to parse the +CMT header line
# +CMT: "+639171234567","","26/04/13,21:00:00+32"
_CMT_HEADER_RE = re.compile(r'^\+CMT:\s*"([^"]*)",[^,]*,"([^"]*)"')

# Regex to parse +CMGL index lines for draining stored messages
# +CMGL: 1,"REC UNREAD","+639171234567","","26/04/13,21:00:00+32"
_CMGL_HEADER_RE = re.compile(r'^\+CMGL:\s*(\d+),[^,]*,"([^"]*)",[^,]*,"([^"]*)"')


def _parse_cmt_header(line: str):
    """
    Parse +CMT header. Returns (from_number, received_at) or None.
    """
    m = _CMT_HEADER_RE.match(line.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboundListener(threading.Thread):
    """
    One instance per active modem port. Runs for the lifetime of the process.

    Configures AT+CNMI on the port and blocks on readline(), waking only
    when the modem pushes a +CMT unsolicited notification.
    """

    def __init__(
        self,
        port: str,
        runtime_sim_id: str,
        spool: InboundSpool,
        webhook_url: str,
        max_webhook_attempts: int = 10,
        baudrate: int = 115200,
    ) -> None:
        super().__init__(name=f"inbound-{port}", daemon=True)
        self._port = port
        self._runtime_sim_id = runtime_sim_id
        self._spool = spool
        self._webhook_url = webhook_url
        self._max_webhook_attempts = max_webhook_attempts
        self._baudrate = baudrate
        self._stop_event = threading.Event()
        self._ser: Optional[serial.Serial] = None

    # ------------------------------------------------------------------
    # Thread entry
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("INBOUND_LISTENER_START port=%s sim=%s", self._port, self._runtime_sim_id)

        while not self._stop_event.is_set():
            try:
                self._run_session()
            except Exception as exc:
                logger.error(
                    "INBOUND_LISTENER_SESSION_ERROR port=%s error=%s — restarting in 10s",
                    self._port, exc,
                )
                self._close_serial()
                if not self._stop_event.wait(10):
                    continue
                break

        logger.info("INBOUND_LISTENER_STOPPED port=%s", self._port)

    def stop(self) -> None:
        self._stop_event.set()
        self._close_serial()

    # ------------------------------------------------------------------
    # Session: open port, configure, drain, listen
    # ------------------------------------------------------------------

    def _run_session(self) -> None:
        self._ser = serial.Serial(
            self._port,
            self._baudrate,
            timeout=1,          # 1s read timeout — lets us check stop_event periodically
        )
        time.sleep(0.5)
        self._ser.reset_input_buffer()

        # Configure modem for text mode + push notifications
        self._cmd("AT")
        self._cmd("AT+CMGF=1")           # text mode
        self._cmd("AT+CNMI=2,2,0,0,0")   # push +CMT immediately on receipt

        # Drain any messages stored on SIM before listener started
        self._drain_stored()

        logger.info("INBOUND_LISTENER_READY port=%s sim=%s", self._port, self._runtime_sim_id)

        # Main loop — block waiting for +CMT unsolicited lines
        buffer = ""
        pending_cmt_from: Optional[str] = None
        pending_cmt_time: Optional[str] = None

        while not self._stop_event.is_set():
            try:
                raw = self._ser.readline()
            except serial.SerialException as exc:
                raise RuntimeError(f"serial read error: {exc}") from exc

            if not raw:
                continue  # timeout — loop to check stop_event

            line = raw.decode("utf-8", errors="ignore").rstrip("\r\n")

            if not line:
                continue

            # Detect +CMT header
            parsed = _parse_cmt_header(line)
            if parsed:
                pending_cmt_from, pending_cmt_time = parsed
                continue

            # If we have a pending CMT header, this line is the message body
            if pending_cmt_from is not None:
                message_body = line
                self._handle_inbound(
                    from_number=pending_cmt_from,
                    message=message_body,
                    received_at=pending_cmt_time or _now_iso(),
                )
                pending_cmt_from = None
                pending_cmt_time = None
                continue

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_inbound(self, from_number: str, message: str, received_at: str) -> None:
        logger.info(
            "INBOUND_RECEIVED sim=%s from=%s",
            self._runtime_sim_id, from_number,
        )

        # 1. Write to spool (durable — safe to delete from SIM after this)
        key = self._spool.insert(
            runtime_sim_id=self._runtime_sim_id,
            from_number=from_number,
            message=message,
            received_at=received_at,
        )

        # 2. Delete from SIM storage (prevents SIM filling up)
        try:
            self._cmd('AT+CMGDA="DEL ALL"')
        except Exception:
            # Non-fatal — message is already in spool
            logger.warning("INBOUND_SIM_DELETE_FAILED sim=%s", self._runtime_sim_id)

        # 3. Attempt immediate delivery to Laravel
        if self._webhook_url:
            record = {
                "idempotency_key": key,
                "runtime_sim_id":  self._runtime_sim_id,
                "from_number":     from_number,
                "message":         message,
                "received_at":     received_at,
                "attempts":        0,
                "last_attempt_at": None,
            }
            deliver_one(
                spool=self._spool,
                record=record,
                webhook_url=self._webhook_url,
                max_attempts=self._max_webhook_attempts,
            )

    # ------------------------------------------------------------------
    # Drain stored messages (AT+CMGL)
    # ------------------------------------------------------------------

    def _drain_stored(self) -> None:
        """
        Read and process any messages already stored on the SIM.
        Called once after AT+CNMI is configured.
        """
        try:
            self._ser.write(b'AT+CMGL="ALL"\r')
            self._ser.flush()
            time.sleep(0.5)

            raw = self._ser.read(4096).decode("utf-8", errors="ignore")
            lines = raw.splitlines()

            i = 0
            while i < len(lines):
                line = lines[i].strip()
                m = _CMGL_HEADER_RE.match(line)
                if m and i + 1 < len(lines):
                    from_number = m.group(2)
                    received_at = m.group(3)
                    message_body = lines[i + 1].strip()
                    if message_body and message_body not in ("OK", "ERROR"):
                        self._handle_inbound(
                            from_number=from_number,
                            message=message_body,
                            received_at=received_at,
                        )
                    i += 2
                else:
                    i += 1

        except Exception as exc:
            logger.warning("INBOUND_DRAIN_FAILED port=%s error=%s", self._port, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cmd(self, command: str, timeout: float = 3.0) -> str:
        """Send an AT command and read until OK/ERROR or timeout."""
        if not self._ser or not self._ser.is_open:
            raise RuntimeError("serial port not open")

        self._ser.write(f"{command}\r".encode("utf-8"))
        self._ser.flush()

        deadline = time.monotonic() + timeout
        buffer = ""

        while time.monotonic() < deadline:
            raw = self._ser.readline()
            if raw:
                line = raw.decode("utf-8", errors="ignore")
                buffer += line
                stripped = line.strip()
                if stripped in ("OK", "ERROR") or stripped.startswith("+CME ERROR") or stripped.startswith("+CMS ERROR"):
                    break

        return buffer

    def _close_serial(self) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
