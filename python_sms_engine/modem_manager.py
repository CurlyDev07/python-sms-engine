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
        results: List[ModemHealthItem] = []
        modems = self.registry.get_all()

        for modem in modems:
            started_at = time.monotonic()
            sim_id = modem.get("sim_id")
            port = str(modem.get("port") or "")
            at_ok = bool(modem.get("at_ok"))
            reachable = bool(port)
            error = None if at_ok else "AT_NOT_RESPONDING"

            duration_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "MODEM_HEALTH_CHECK sim_id=%s port=%s phone=%s duration_ms=%s error=%s",
                sim_id,
                port,
                None,
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
