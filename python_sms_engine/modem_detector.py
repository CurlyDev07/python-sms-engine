import glob
import os
import time
from typing import Any, Dict, List, Optional

from at_client import ModemATClient


def _extract_signal(raw: str) -> Optional[str]:
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("+CSQ:"):
            return stripped
    return None


def _is_registered(creg_response: str) -> bool:
    if not creg_response:
        return False
    return "+CREG: 0,1" in creg_response or "+CREG: 0,5" in creg_response


def _probe_modem(port: str, serial_timeout: float, command_timeout: float) -> Dict[str, Any]:
    client = ModemATClient(
        port=port,
        serial_timeout=serial_timeout,
        command_timeout=command_timeout,
    )

    at_ok = False
    sim_ready = False
    creg_registered = False
    signal: Optional[str] = None

    opened = False

    try:
        client.open()
        opened = True

        # AT
        try:
            at_resp = client._command_expect_ok(
                "AT",
                "AT_NOT_RESPONDING",
                deadline=time.monotonic() + command_timeout,
                retries=0,
            )
            at_ok = "OK" in at_resp
        except Exception:
            at_ok = False

        if not at_ok:
            return {
                "port": port,
                "at_ok": False,
                "sim_ready": False,
                "creg_registered": False,
                "signal": None,
            }

        # CPIN
        try:
            cpin = client._command_expect_ok(
                "AT+CPIN?",
                "AT_NOT_RESPONDING",
                deadline=time.monotonic() + command_timeout,
                retries=0,
            )
            sim_ready = "READY" in cpin
        except Exception:
            sim_ready = False

        # CREG
        try:
            creg = client._command_expect_ok(
                "AT+CREG?",
                "AT_NOT_RESPONDING",
                deadline=time.monotonic() + command_timeout,
                retries=0,
            )
            creg_registered = _is_registered(creg)
        except Exception:
            creg_registered = False

        # SIGNAL
        try:
            client._write(b"AT+CSQ\r", timeout_code="AT_NOT_RESPONDING")
            csq = client._read_until(
                expected=["+CSQ:", "OK"],
                failure=["ERROR"],
                timeout=command_timeout,
                timeout_code="AT_NOT_RESPONDING",
            )
            signal = _extract_signal(csq)
        except Exception:
            signal = None

    finally:
        if opened:
            client.close()

    return {
        "port": port,
        "at_ok": at_ok,
        "sim_ready": sim_ready,
        "creg_registered": creg_registered,
        "signal": signal,
    }


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict[str, Any]]:
    modems: List[Dict[str, Any]] = []

    device_ids = sorted(glob.glob("/dev/serial/by-id/*"))

    grouped: Dict[str, List[str]] = {}

    # 🔥 GROUP PER PHYSICAL MODEM
    for dev in device_ids:
        base = os.path.basename(dev)
        physical = base.split("-if", 1)[0]
        grouped.setdefault(physical, []).append(dev)

    for physical_id, devs in grouped.items():

        # 🔥 PRIORITY ORDER
        priority_order = ["if02", "if01", "if00", "if03"]

        sorted_devs = sorted(
            devs,
            key=lambda d: next((i for i, p in enumerate(priority_order) if p in d), 99)
        )

        print(f"[GROUP] {physical_id} → {sorted_devs}")

        # 🔥 TRY ONE BY ONE, STOP ON FIRST SUCCESS
        for dev in sorted_devs:
            try:
                port = os.path.realpath(dev)

                if not port.startswith("/dev/ttyUSB"):
                    continue

                if not os.path.exists(port):
                    continue

                result = _probe_modem(
                    port=port,
                    serial_timeout=serial_timeout,
                    command_timeout=command_timeout,
                )

                print(f"[TRY] {port} → {result}")

                if result["creg_registered"]:
                    print(f"[SELECTED] {port} ✅")

                    modems.append({
                        "sim_id": hash(port) & 0xFFFFFFFF,
                        "device_id": dev,
                        "port": port,
                        "at_ok": result["at_ok"],
                        "sim_ready": result["sim_ready"],
                        "creg_registered": result["creg_registered"],
                        "signal": result["signal"],
                    })

                    break  # 🔥 CRITICAL: STOP HERE

            except Exception as e:
                print(f"[ERROR] {dev} → {str(e)}")
                continue

    return modems