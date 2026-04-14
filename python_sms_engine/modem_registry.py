import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from modem_detector import PROBE_TIMEOUT_S, detect_modems, discover_all_modems

logger = logging.getLogger("python_sms_engine.modem_registry")

# Number of consecutive probe failures required before downgrading effective_send_ready.
# A single bad probe (transient AT noise, brief CREG drop) will NOT flip the UI.
_DOWNGRADE_THRESHOLD = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readiness_reason(modem: Dict) -> str:
    """Human-readable code explaining why a modem is not realtime-ready."""
    if modem.get("probe_error"):
        err = modem["probe_error"]
        if "TIMEOUT" in err:
            return "PROBE_TIMEOUT"
        return err
    if not modem.get("at_ok"):
        return "AT_NOT_RESPONDING"
    if not modem.get("sim_ready"):
        return "SIM_NOT_READY"
    if not modem.get("creg_registered"):
        return "CREG_NOT_REGISTERED"
    if modem.get("identifier_source") != "imsi":
        return "IMSI_UNAVAILABLE"
    return "UNKNOWN"


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

        # Persistent per-device state keyed by device_id (USB physical address).
        # Survives individual bad probes — provides hysteresis and identity memory.
        self._device_state: Dict[str, Dict[str, Any]] = {}

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

    def _init_device_state(self, device_id: str) -> Dict[str, Any]:
        """Return existing device state or create a fresh entry."""
        if device_id not in self._device_state:
            self._device_state[device_id] = {
                "last_good_imsi":            None,
                "last_good_probe_at":        None,
                "consecutive_probe_failures": 0,
                "effective_send_ready":      False,
            }
        return self._device_state[device_id]

    def _apply_hysteresis(self, modem: Dict) -> Dict:
        """
        Merge a fresh probe result with per-device persistent state.

        Rules:
          - Good probe  → reset failure counter, update last_good_imsi, immediately
                          upgrade effective_send_ready to True.
          - Bad probe   → increment failure counter; only downgrade effective_send_ready
                          after _DOWNGRADE_THRESHOLD consecutive failures.
          - Identity    → if current probe has no IMSI but we have a last_good_imsi for
                          this device_id, use it (prevents sim_id identity flapping).

        Adds diagnostic fields to the modem dict (non-destructive — additive only).
        Returns the enriched modem dict.
        """
        device_id = modem.get("device_id")
        if not device_id:
            # No device_id — can't key state; return as-is with defaults
            modem.setdefault("probe_timestamp",              _now_iso())
            modem.setdefault("consecutive_probe_failures",   0)
            modem.setdefault("last_good_probe_at",           None)
            modem.setdefault("last_good_imsi",               None)
            modem.setdefault("realtime_probe_ready",         modem.get("send_ready", False))
            modem.setdefault("effective_send_ready",         modem.get("send_ready", False))
            modem.setdefault("identifier_source_confidence", "low")
            modem.setdefault("readiness_reason_code",        None)
            return modem

        state = self._init_device_state(device_id)
        realtime_ready = bool(modem.get("send_ready", False))

        # --- identity recovery ---
        current_imsi = modem.get("imsi")
        if current_imsi:
            # Fresh IMSI from this probe
            if state["last_good_imsi"] and state["last_good_imsi"] != current_imsi:
                logger.info(
                    "IDENTIFIER_SOURCE_CHANGED old=%s new=%s device_id=%s",
                    state["last_good_imsi"], current_imsi, device_id,
                )
            state["last_good_imsi"] = current_imsi
            identifier_source_confidence = "high"
        elif state["last_good_imsi"]:
            # Probe returned no IMSI — restore from last known good
            recovered_imsi = state["last_good_imsi"]
            modem["imsi"]             = recovered_imsi
            modem["sim_id"]           = recovered_imsi
            modem["identifier_source"] = "imsi"
            identifier_source_confidence = "medium"
            logger.info(
                "IDENTIFIER_RECOVERED_FROM_CACHE device_id=%s imsi=%s",
                device_id, recovered_imsi,
            )
        else:
            identifier_source_confidence = "low"

        # Recompute realtime send_ready after possible identity recovery
        realtime_ready = bool(
            not modem.get("probe_error")
            and modem.get("at_ok")
            and modem.get("sim_ready")
            and modem.get("creg_registered")
            and modem.get("identifier_source") == "imsi"
        )
        modem["send_ready"] = realtime_ready

        # --- hysteresis state machine ---
        old_effective = state["effective_send_ready"]

        if realtime_ready:
            state["consecutive_probe_failures"] = 0
            state["last_good_probe_at"] = _now_iso()
            new_effective = True
        else:
            state["consecutive_probe_failures"] += 1
            failures = state["consecutive_probe_failures"]
            if old_effective and failures >= _DOWNGRADE_THRESHOLD:
                new_effective = False
            else:
                new_effective = old_effective  # hold steady until threshold

        if old_effective != new_effective:
            reason = _readiness_reason(modem) if not new_effective else "PROBE_SUCCESS"
            logger.info(
                "MODEM_READY_STATE_CHANGED old=%s new=%s reason=%s "
                "consecutive_failures=%s device_id=%s sim_id=%s",
                old_effective, new_effective, reason,
                state["consecutive_probe_failures"], device_id, modem.get("sim_id"),
            )

        state["effective_send_ready"] = new_effective

        # --- attach diagnostic fields ---
        modem["probe_timestamp"]              = _now_iso()
        modem["consecutive_probe_failures"]   = state["consecutive_probe_failures"]
        modem["last_good_probe_at"]           = state["last_good_probe_at"]
        modem["last_good_imsi"]               = state["last_good_imsi"]
        modem["realtime_probe_ready"]         = realtime_ready
        modem["effective_send_ready"]         = new_effective
        modem["identifier_source_confidence"] = identifier_source_confidence
        modem["readiness_reason_code"]        = None if realtime_ready else _readiness_reason(modem)

        return modem

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

        Applies per-device hysteresis:
          - effective_send_ready only downgrades after 3 consecutive failures
          - last known good IMSI is restored when a probe returns no identity
          - diagnostic fields (consecutive_probe_failures, last_good_probe_at, etc.)
            are added to every row

        Also updates the routing cache with healthy modems found so that
        subsequent /send calls benefit from the fresh state.

        Bounded by probe_timeout — never hangs the caller.
        """
        all_probed = discover_all_modems(
            serial_timeout=self.serial_timeout,
            command_timeout=self.command_timeout,
            probe_timeout=probe_timeout,
        )

        # Apply hysteresis and identity recovery to every probed modem
        enriched = [self._apply_hysteresis(modem) for modem in all_probed]

        # Update routing cache with effectively-ready modems
        new_cache: Dict[str, Dict[str, Any]] = {}
        for modem in enriched:
            if modem.get("effective_send_ready"):
                sim_id = modem.get("sim_id")
                if sim_id:
                    new_cache[str(sim_id)] = modem

        with self._refresh_lock:
            self._cache = new_cache
            self._last_refresh = time.monotonic()

        print(f"[REGISTRY] discover() found {len(enriched)} ports, {len(new_cache)} effective-ready")
        return enriched

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
