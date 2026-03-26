import time
from typing import Any, Dict, List, Optional

from modem_detector import detect_modems


class ModemRegistry:
    def __init__(
        self,
        serial_timeout: float,
        command_timeout: float,
        refresh_ttl: float = 10.0,  # 🔥 auto refresh every X seconds
    ):
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout
        self.refresh_ttl = refresh_ttl

        self._cache: Dict[str, Dict[str, Any]] = {}  # 🔥 O(1 lookup
        self._last_refresh: float = 0.0

    def _should_refresh(self) -> bool:
        return (time.monotonic() - self._last_refresh) > self.refresh_ttl

    def refresh(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        if not force and not self._should_refresh():
            return self._cache

        print("[REGISTRY] Refreshing modems...")

        modems = detect_modems(
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
        )

        new_cache: Dict[str, Dict[str, Any]] = {}

        for modem in modems:
            sim_id = modem.get("sim_id")

            if not sim_id:
                continue

            new_cache[str(sim_id)] = modem

        self._cache = new_cache
        self._last_refresh = time.monotonic()

        print(f"[REGISTRY] Loaded {len(self._cache)} modems")

        return self._cache

    def get_all(self) -> List[Dict[str, Any]]:
        self.refresh()
        return list(self._cache.values())

    def get_by_sim_id(self, sim_id: str) -> Optional[Dict[str, Any]]:
        self.refresh()

        modem = self._cache.get(str(sim_id))

        if modem:
            print(f"[REGISTRY HIT] {sim_id} → {modem.get('port')}")
            return modem

        print(f"[REGISTRY MISS] {sim_id}")
        return None