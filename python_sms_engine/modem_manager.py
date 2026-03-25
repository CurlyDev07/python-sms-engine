import logging
import time
from typing import Dict, List

from at_client import ModemATClient
from schemas import ModemHealthItem

logger = logging.getLogger("python_sms_engine")


class ModemManager:
    def __init__(self, sim_map: Dict[int, str], serial_timeout: float, command_timeout: float) -> None:
        self.sim_map = sim_map
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout

    def update_sim_map(self, sim_map: Dict[int, str]) -> None:
        self.sim_map = sim_map

    def health(self) -> List[ModemHealthItem]:
        results: List[ModemHealthItem] = []

        for sim_id, port in self.sim_map.items():
            started_at = time.monotonic()
            reachable = False
            at_ok = False
            error = None

            try:
                client = ModemATClient(
                    port=port,
                    serial_timeout=self.serial_timeout,
                    command_timeout=self.command_timeout,
                )
                probe = client.probe(timeout=self.command_timeout)
                reachable = probe["reachable"]
                at_ok = probe["at_ok"]
                if not at_ok:
                    error = "AT_NOT_RESPONDING"
            except Exception:
                reachable = False
                at_ok = False
                error = "CHECK_FAILED"

            duration_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "MODEM_HEALTH_CHECK sim_id=%s port=%s phone=%s duration_ms=%s error=%s",
                sim_id,
                port,
                None,
                duration_ms,
                error,
            )
            results.append(ModemHealthItem(sim_id=sim_id, port=port, reachable=reachable, at_ok=at_ok))

        return results
