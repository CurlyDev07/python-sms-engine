import glob
import hashlib
import os
import time
from typing import Any, Dict, List, Optional

from at_client import ModemATClient


def _stable_sim_id(device_id: str) -> int:
    # Deterministic integer derived from stable /dev/serial/by-id path.
    digest = hashlib.sha1(device_id.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16)


def _extract_signal(raw: str) -> Optional[str]:
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("+CSQ:"):
            return stripped
    return None


def _probe_modem(device_id: str, port: str, serial_timeout: float, command_timeout: float) -> Dict[str, Any]:
    at_ok = False
    sim_ready = False
    creg_registered = False
    signal: Optional[str] = None

    client = ModemATClient(
        port=port,
        serial_timeout=serial_timeout,
        command_timeout=command_timeout,
    )

    opened = False
    try:
        client.open()
        opened = True

        try:
            at_response = client._command_expect_ok(
                "AT",
                "AT_NOT_RESPONDING",
                deadline=time.monotonic() + command_timeout,
                retries=0,
            )
            print(f"[AT RESPONSE] {device_id} → {at_response}")
            if at_response and "OK" in at_response:
                at_ok = True
            else:
                at_ok = False
        except Exception:
            at_ok = False

        if at_ok:
            try:
                cpin_response = client._command_expect_ok(
                    "AT+CPIN?",
                    "AT_NOT_RESPONDING",
                    deadline=time.monotonic() + command_timeout,
                    retries=0,
                )
                print(f"[CPIN RESPONSE] {device_id} → {cpin_response}")
                if cpin_response and "READY" in cpin_response:
                    sim_ready = True
                else:
                    sim_ready = False
            except Exception:
                sim_ready = False

            try:
                client._write(b"AT+CSQ\r", timeout_code="AT_NOT_RESPONDING")
                csq_response = client._read_until(
                    expected=["+CSQ:", "OK"],
                    failure=["ERROR", "+CMS ERROR", "+CME ERROR"],
                    timeout=command_timeout,
                    timeout_code="AT_NOT_RESPONDING",
                )
                signal = _extract_signal(csq_response)
            except Exception:
                signal = None

            if not sim_ready and signal:
                sim_ready = True

            try:
                creg_response = client._command_expect_ok(
                    "AT+CREG?",
                    "AT_NOT_RESPONDING",
                    deadline=time.monotonic() + command_timeout,
                    retries=0,
                )
                print(f"[CREG RESPONSE] {device_id} → {creg_response}")

                if "0,1" in creg_response or "0,5" in creg_response:
                    creg_registered = True

            except Exception:
                creg_registered = False
    finally:
        if opened:
            client.close()

    return {
        "sim_id": _stable_sim_id(device_id),
        "device_id": device_id,
        "port": port,
        "at_ok": at_ok,
        "sim_ready": sim_ready,
        "creg_registered": creg_registered,
        "signal": signal,
    }


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict[str, Any]]:
    modems: List[Dict[str, Any]] = []
    grouped: Dict[str, List[str]] = {}
    device_ids = sorted(glob.glob("/dev/serial/by-id/*"))

    for device_id in device_ids:
        base = os.path.basename(device_id)
        physical_id = base.split("-if", 1)[0]
        grouped.setdefault(physical_id, []).append(device_id)

    for _, group_device_ids in grouped.items():
        candidates: List[Dict[str, Any]] = []

        for device_id in sorted(group_device_ids):
            try:
                port = os.path.realpath(device_id)
            except Exception:
                port = device_id

            # Guard against malformed device paths and enforce ttyUSB usage.
            if port.startswith("/ev/ttyUSB"):
                port = port.replace("/ev/ttyUSB", "/dev/ttyUSB", 1)
            if not port.startswith("/dev/ttyUSB"):
                continue

            if not os.path.exists(port):
                continue

            try:
                modem = _probe_modem(
                    device_id=device_id,
                    port=port,
                    serial_timeout=serial_timeout,
                    command_timeout=command_timeout,
                )
                if modem.get("at_ok"):
                    candidates.append(modem)
            except Exception as e:
                print(f"[MODEM ERROR] {device_id} → {str(e)}")
                continue

        if not candidates:
            continue

        candidates = [m for m in candidates if m.get("creg_registered")]

        if not candidates:
            continue

        def _score(item: Dict[str, Any]) -> tuple:
            creg = bool(item.get("creg_registered"))
            sim_ready = bool(item.get("sim_ready"))
            signal = bool(item.get("signal"))
            at_ok = bool(item.get("at_ok"))

            return (
                1 if creg else 0,
                1 if sim_ready else 0,
                1 if signal else 0,
                1 if at_ok else 0,
            )

        best = max(candidates, key=_score)
        modems.append(best)

    return modems
