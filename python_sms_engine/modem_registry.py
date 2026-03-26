import time
from typing import Any, Dict, List

from modem_detector import detect_modems

MODEM_REGISTRY_TTL_SECONDS = 10.0


class ModemRegistry:
    def __init__(
        self,
        serial_timeout: float,
        command_timeout: float,
        ttl_seconds: float = MODEM_REGISTRY_TTL_SECONDS,
    ) -> None:
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout
        self.ttl = ttl_seconds

        self._cache: List[Dict[str, Any]] = []
        self._last_refresh: float = 0.0

    def refresh(self) -> List[Dict[str, Any]]:
        self._cache = detect_modems(
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
        )
        self._last_refresh = time.monotonic()
        return self._cache

    def _is_expired(self) -> bool:
        if not self._cache:
            return True
        return (time.monotonic() - self._last_refresh) >= self.ttl

    def get_all(self) -> List[Dict[str, Any]]:
        if self._is_expired():
            return self.refresh()
        return self._cache

    def get_by_sim_id(self, sim_id: int) -> Dict[str, Any]:
        # 🔥 First try cached
        modems = self.get_all()

        for modem in modems:
            if (
                modem.get("sim_id") == sim_id
                and modem.get("at_ok")
                and modem.get("sim_ready")
            ):
                return modem

        # 🔥 Retry with fresh scan (important for plug/unplug recovery)
        modems = self.refresh()

        for modem in modems:
            if (
                modem.get("sim_id") == sim_id
                and modem.get("at_ok")
                and modem.get("sim_ready")
            ):
                return modem

        # ❌ Final fail (clean + explicit)
        raise Exception("SIM_NOT_FOUND")