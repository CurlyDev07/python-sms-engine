import logging
import time
from typing import List

from modem_registry import ModemRegistry
from schemas import ModemHealthItem

logger = logging.getLogger("python_sms_engine")


class ModemManager:
    def __init__(self, registry: ModemRegistry) -> None:
        self.registry = registry

    def health(self) -> List[ModemHealthItem]:
        """
        Returns current modem health status.
        Uses IMSI-based identity (string sim_id).
        """
        results: List[ModemHealthItem] = []
        modems = self.registry.get_all()

        for modem in modems:
            started_at = time.monotonic()

            sim_id = str(modem.get("sim_id")) if modem.get("sim_id") else None
            port = str(modem.get("port") or "")
            at_ok = bool(modem.get("at_ok"))
            reachable = bool(port)

            error = None if at_ok else "AT_NOT_RESPONDING"

            duration_ms = int((time.monotonic() - started_at) * 1000)

            logger.info(
                "MODEM_HEALTH_CHECK sim_id=%s port=%s duration_ms=%s error=%s",
                sim_id,
                port,
                duration_ms,
                error,
            )

            results.append(
                ModemHealthItem(
                    sim_id=sim_id,
                    port=port,
                    reachable=reachable,
                    at_ok=at_ok,
                )
            )

        return results

    def summary(self) -> dict:
        """
        Quick summary for monitoring / dashboard
        """
        modems = self.registry.get_all()

        total = len(modems)
        online = sum(1 for m in modems if m.get("at_ok"))
        offline = total - online

        return {
            "total": total,
            "online": online,
            "offline": offline,
        }

    def get_available_modems(self) -> List[dict]:
        """
        Returns only usable modems (ready for SMS sending)
        """
        modems = self.registry.get_all()

        available = []

        for modem in modems:
            if (
                modem.get("at_ok")
                and modem.get("sms_capable")
                and modem.get("sim_ready")
                and modem.get("creg_registered")
            ):
                available.append(modem)

        return available

    def debug_dump(self) -> List[dict]:
        """
        Full raw modem dump (for debugging only)
        """
        return self.registry.get_all()