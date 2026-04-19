"""
Modem watchdog — pings each persistent connection every 30s.

If the AT ping fails (modem frozen, port stuck), it closes and reinitializes
the persistent connection automatically. If reinit also fails, it logs an alert
and leaves the client in a failed state so the next send triggers lazy reinit.

The watchdog acquires the per-port lock before pinging — it never races with
an active send. If a send is in progress it skips that port and retries next cycle.
"""

import logging
import threading
import time

from at_client import SMSExecutionError, get_port_lock

logger = logging.getLogger("python_sms_engine.watchdog")


class ModemWatchdog(threading.Thread):
    def __init__(
        self,
        service,
        registry,
        interval: float = 30.0,
    ) -> None:
        super().__init__(name="modem-watchdog", daemon=True)
        self._service = service
        self._registry = registry
        self._interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("WATCHDOG_STARTED interval=%ss", self._interval)
        while not self._stop_event.wait(self._interval):
            self._ping_all()

    def _ping_all(self) -> None:
        modems = self._registry.get_all()
        logger.info("WATCHDOG_PING_ALL modem_count=%s known_ports=%s", len(modems), list(self._service._clients.keys()))
        for modem in modems:
            port = modem.get("port")
            sim_id = modem.get("sim_id")
            if not port or not sim_id:
                continue
            self._ping_one(port, sim_id)

    def _ping_one(self, port: str, sim_id: str) -> None:
        with self._service._clients_lock:
            client = self._service._clients.get(port)

        if client is None:
            logger.warning("WATCHDOG_NO_CLIENT port=%s sim_id=%s — skipping", port, sim_id)
            return  # not warmed up yet — skip

        port_lock = get_port_lock(port)
        if not port_lock.acquire(timeout=5.0):
            logger.info("WATCHDOG_SKIP port=%s sim_id=%s reason=lock_busy", port, sim_id)
            return

        try:
            deadline = time.monotonic() + 5.0
            client._command_expect_ok("AT", "AT_NOT_RESPONDING", deadline=deadline)
            logger.info("WATCHDOG_OK port=%s sim_id=%s", port, sim_id)

        except Exception as exc:
            logger.warning(
                "WATCHDOG_FAIL port=%s sim_id=%s error=%s — reinitializing",
                port, sim_id, exc,
            )
            try:
                client._initialized = False
                client.close()
                client.initialize(global_timeout=20.0)
                logger.info("WATCHDOG_RECOVERED port=%s sim_id=%s", port, sim_id)
            except Exception as reinit_exc:
                logger.error(
                    "WATCHDOG_RECOVERY_FAILED port=%s sim_id=%s error=%s",
                    port, sim_id, reinit_exc,
                )

        finally:
            port_lock.release()

    def stop(self) -> None:
        self._stop_event.set()
