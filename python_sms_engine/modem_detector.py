import glob
import os
import re
import time
from typing import Dict, List, Optional

from at_client import ModemATClient


def _extract_signal(raw: str) -> Optional[str]:
    if not raw:
        return None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("+CSQ:"):
            return s
    return None


def _extract_first_line(raw: str) -> Optional[str]:
    if not raw:
        return None
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s in ("OK", "ERROR"):
            continue
        if s.startswith("AT"):
            continue
        return s
    return None


def _is_registered(raw: str) -> bool:
    if not raw:
        return False
    compact = raw.replace(" ", "")
    return "+CREG:0,1" in compact or "+CREG:0,5" in compact


def _parse_ttyusb_num(port: str) -> int:
    m = re.search(r"ttyUSB(\d+)$", port)
    if not m:
        return 999999
    return int(m.group(1))


def _cmd_expect_ok(client: ModemATClient, command: str, timeout: float) -> str:
    return client._command_expect_ok(
        command,
        "AT_NOT_RESPONDING",
        time.monotonic() + timeout,
    )


def _wait_for_cpin_ready(client: ModemATClient, timeout: float = 4.0) -> bool:
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            client._write(b"AT+CPIN?\r", timeout_code="AT_NOT_RESPONDING")
            resp = client._read_until(
                expected=["OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=1.5,
                timeout_code="AT_NOT_RESPONDING",
            )
            if "+CPIN:READY" in resp.replace(" ", "").upper():
                return True
        except Exception:
            pass

        time.sleep(0.3)

    return False


def _wait_for_creg(client: ModemATClient, timeout: float = 4.0) -> bool:
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            client._write(b"AT+CREG?\r", timeout_code="AT_NOT_RESPONDING")
            resp = client._read_until(
                expected=["OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=1.5,
                timeout_code="AT_NOT_RESPONDING",
            )
            if _is_registered(resp):
                return True
        except Exception:
            pass

        time.sleep(0.5)

    return False


def _get_identity(client: ModemATClient, timeout: float = 2.0) -> Dict[str, Optional[str]]:
    data = {
        "imsi": None,
        "iccid": None,
        "imei": None,
    }

    try:
        resp = _cmd_expect_ok(client, "AT+CIMI", timeout)
        data["imsi"] = _extract_first_line(resp)
    except Exception:
        pass

    try:
        resp = _cmd_expect_ok(client, "AT+CCID", timeout)
        val = _extract_first_line(resp)
        if val:
            data["iccid"] = val.replace("+CCID:", "").strip()
    except Exception:
        pass

    try:
        resp = _cmd_expect_ok(client, "AT+GSN", timeout)
        data["imei"] = _extract_first_line(resp)
    except Exception:
        pass

    return data


def _probe_port(port: str, serial_timeout: float, command_timeout: float) -> Dict:
    client = ModemATClient(
        port=port,
        serial_timeout=serial_timeout,
        command_timeout=command_timeout,
    )

    result = {
        "port": port,
        "at_ok": False,
        "sim_ready": False,
        "creg_registered": False,
        "signal": None,
        "imsi": None,
        "iccid": None,
        "imei": None,
        "score": 0,
    }

    opened = False

    try:
        client.open()
        opened = True

        time.sleep(0.5)

        if client._serial:
            client._serial.reset_input_buffer()
            client._serial.reset_output_buffer()

        try:
            resp = _cmd_expect_ok(client, "AT", command_timeout)
            result["at_ok"] = "OK" in resp
        except Exception:
            return result

        try:
            _cmd_expect_ok(client, "ATE0", command_timeout)
        except Exception:
            pass

        result["sim_ready"] = _wait_for_cpin_ready(client)
        result["creg_registered"] = _wait_for_creg(client)

        try:
            client._write(b"AT+CSQ\r", timeout_code="AT_NOT_RESPONDING")
            csq = client._read_until(
                expected=["OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=command_timeout,
                timeout_code="AT_NOT_RESPONDING",
            )
            result["signal"] = _extract_signal(csq)
        except Exception:
            pass

        identity = _get_identity(client)
        result.update(identity)

        score = 0
        if result["at_ok"]:
            score += 1
        if result["imei"]:
            score += 2
        if result["sim_ready"]:
            score += 4
        if result["creg_registered"]:
            score += 4
        if result["imsi"]:
            score += 3
        if result["iccid"]:
            score += 3
        result["score"] = score

        return result

    finally:
        if opened:
            client.close()


def _build_strict_quectel_groups() -> Dict[str, Dict[str, str]]:
    """
    Strict grouping:
    - enumerate only /dev/serial/by-id/usb-Quectel*
    - group by physical modem prefix before '-if'
    - keep only if02 and if03
    """
    groups: Dict[str, Dict[str, str]] = {}

    by_id_paths = sorted(glob.glob("/dev/serial/by-id/usb-Quectel*"))

    for dev in by_id_paths:
        base = os.path.basename(dev)

        if "-if02-" in base:
            physical = base.split("-if02-", 1)[0]
            groups.setdefault(physical, {})
            groups[physical]["if02"] = os.path.realpath(dev)

        elif "-if03-" in base:
            physical = base.split("-if03-", 1)[0]
            groups.setdefault(physical, {})
            groups[physical]["if03"] = os.path.realpath(dev)

    return groups


def _select_sim_id(item: Dict) -> Optional[str]:
    return item.get("imsi") or item.get("iccid") or item.get("imei")


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict]:
    groups = _build_strict_quectel_groups()
    modems: List[Dict] = []

    for physical_modem, ports in sorted(groups.items()):
        print(f"[USB MODEM] {physical_modem} -> {ports}")

        primary_port = ports.get("if02")
        fallback_port = ports.get("if03")

        best_probe = None
        best_interface = None

        # STRICT RULE: probe if02 first
        if primary_port and os.path.exists(primary_port):
            probe = _probe_port(primary_port, serial_timeout, command_timeout)
            print(f"[TRY] {physical_modem} if02 -> {probe}")

            if probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]:
                best_probe = probe
                best_interface = "if02"

        # STRICT RULE: only fallback to if03 if if02 failed
        if best_probe is None and fallback_port and os.path.exists(fallback_port):
            probe = _probe_port(fallback_port, serial_timeout, command_timeout)
            print(f"[TRY] {physical_modem} if03 -> {probe}")

            if probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]:
                best_probe = probe
                best_interface = "if03"

        if best_probe is None:
            continue

        sim_id = _select_sim_id(best_probe)
        if not sim_id:
            continue

        modems.append(
            {
                "sim_id": str(sim_id),
                "device_id": physical_modem,
                "port": best_probe["port"],
                "interface": best_interface,
                "at_ok": best_probe["at_ok"],
                "sim_ready": best_probe["sim_ready"],
                "creg_registered": best_probe["creg_registered"],
                "signal": best_probe["signal"],
                "imsi": best_probe["imsi"],
                "iccid": best_probe["iccid"],
                "imei": best_probe["imei"],
            }
        )

        print(f"[SELECTED] {physical_modem} -> {best_interface} -> {best_probe['port']}")

    return modems