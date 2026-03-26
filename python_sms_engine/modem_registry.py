import time
from typing import Any, Dict, List, Optional

from modem_detector import detect_modems


class ModemRegistry:
    def __init__(self, serial_timeout: float, command_timeout: float):
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout
        self._cache: List[Dict[str, Any]] = []
        self._last_refresh: float = 0.0

    def refresh(self) -> List[Dict[str, Any]]:
        print("[REGISTRY] Refreshing modems...")
        self._cache = detect_modems(
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
        )
        self._last_refresh = time.monotonic()
        return self._cache

    def get_all(self) -> List[Dict[str, Any]]:
        return self._cache

    def get_by_sim_id(self, sim_id: int) -> Optional[Dict[str, Any]]:
        for modem in self._cache:
            if modem.get("sim_id") == sim_id:
                print(f"[REGISTRY HIT] {sim_id} → {modem.get('port')}")
                return modem

        print(f"[REGISTRY MISS] {sim_id}")
        return None