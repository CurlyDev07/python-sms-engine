import os
import threading
import time
from typing import Any, Dict, List, Optional

from modem_detector import PROBE_TIMEOUT_S, detect_modems, discover_all_modems


class ModemRegistry:
    def __init__(
        self,
        serial_timeout: float,
        command_timeout: float,
        refresh_ttl: float = 10.0,
    ):
        self.serial_timeout = serial_timeout
        self.command_timeout = command_timeout
        self.refresh_ttl = refresh_ttl

        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_refresh: float = 0.0
        self._refresh_lock = threading.Lock()

    def _should_refresh(self) -> bool:
        return (time.monotonic() - self._last_refresh) > self.refresh_ttl

    def _all_ports_present(self) -> bool:
        """
        Warm-refresh check: verify every cached modem's port still exists on the filesystem.

        This is essentially free (no serial I/O). It covers the common case where
        nothing has changed — the modems are still plugged in and the ports are stable.

        Returns False (triggering a full re-scan) only when a port disappears, which
        means a modem was physically unplugged or the kernel reassigned the device node.
        """
        for sim_id, modem in self._cache.items():
            port = modem.get("port")
            if not port or not os.path.exists(port):
                print(f"[REGISTRY] Port gone: sim_id={sim_id} port={port} — full rescan needed")
                return False
        return True

    def refresh(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        if not force and not self._should_refresh():
            return self._cache

        with self._refresh_lock:
            # Re-check inside the lock — another thread may have just refreshed
            if not force and not self._should_refresh():
                return self._cache

            # Warm refresh: cache is populated and all ports still exist.
            # Skip full sysfs scan + probe — just update the TTL timestamp.
            if not force and self._cache and self._all_ports_present():
                print(f"[REGISTRY] Warm refresh — {len(self._cache)} modems still present, skipping scan")
                self._last_refresh = time.monotonic()
                return self._cache

            # Cold or forced refresh: full sysfs enumeration + probe
            print("[REGISTRY] Full scan starting...")

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

    def discover(self, probe_timeout: float = PROBE_TIMEOUT_S) -> List[Dict[str, Any]]:
        """
        Probes all modems in parallel and returns ALL results including unhealthy
        and timed-out ones (each has probe_error field set when something went wrong).

        Also updates the routing cache with any healthy modems found so that
        subsequent /send calls benefit from the fresh state.

        Bounded by probe_timeout — never hangs the caller.
        """
        all_probed = discover_all_modems(
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
            probe_timeout=probe_timeout,
        )

        # Update routing cache with healthy modems found in this scan
        new_cache: Dict[str, Dict[str, Any]] = {}
        for modem in all_probed:
            if modem.get("at_ok") and modem.get("sim_ready") and modem.get("creg_registered"):
                sim_id = modem.get("sim_id")
                if sim_id:
                    new_cache[str(sim_id)] = modem

        with self._refresh_lock:
            self._cache = new_cache
            self._last_refresh = time.monotonic()

        print(f"[REGISTRY] discover() found {len(all_probed)} ports, {len(new_cache)} healthy")
        return all_probed

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
