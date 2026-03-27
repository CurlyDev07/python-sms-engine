import glob
import os
import time
from typing import Optional

from at_client import ModemATClient


def _extract_signal(raw: str) -> Optional[str]:
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("+CSQ:"):
            return stripped
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


def _wait_for_cpin_ready(client: ModemATClient, timeout: float = 8.0) -> bool:
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            client._write(b"AT+CPIN?\r", timeout_code="AT_NOT_RESPONDING")
            resp = client._read_until(
                expected=["OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=2.5,
                timeout_code="AT_NOT_RESPONDING",
            )

            compact = resp.replace(" ", "").upper()
            if "+CPIN:READY" in compact:
                return True

        except Exception:
            pass

        time.sleep(0.5)

    return False


def _wait_for_creg(client: ModemATClient, timeout: float = 12.0) -> bool:
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            client._write(b"AT+CREG?\r", timeout_code="AT_NOT_RESPONDING")
            resp = client._read_until(
                expected=["OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=2.5,
                timeout_code="AT_NOT_RESPONDING",
            )

            compact = resp.replace(" ", "")
            if "+CREG:0,1" in compact or "+CREG:0,5" in compact:
                return True

        except Exception:
            pass

        time.sleep(1.0)

    return False


def _get_identity(client: ModemATClient, timeout: float = 5.0):
    data = {
        "imsi": None,
        "iccid": None,
        "imei": None,
    }

    try:
        resp = client._command_expect_ok(
            "AT+CIMI",
            "AT_NOT_RESPONDING",
            time.monotonic() + timeout,
        )
        data["imsi"] = _extract_first_line(resp)
    except Exception:
        pass

    try:
        resp = client._command_expect_ok(
            "AT+CCID",
            "AT_NOT_RESPONDING",
            time.monotonic() + timeout,
        )
        val = _extract_first_line(resp)
        if val:
            data["iccid"] = val.replace("+CCID:", "").strip()
    except Exception:
        pass

    try:
        resp = client._command_expect_ok(
            "AT+GSN",
            "AT_NOT_RESPONDING",
            time.monotonic() + timeout,
        )
        data["imei"] = _extract_first_line(resp)
    except Exception:
        pass

    return data


def _probe_modem(
    port: str,
    device_id: str,
    iface: str,
    serial_timeout: float,
    command_timeout: float,
):
    client = ModemATClient(
        port=port,
        serial_timeout=serial_timeout,
        command_timeout=command_timeout,
    )

    result = {
        "device_id": device_id,
        "port": port,
        "interface": iface,
        "at_ok": False,
        "sim_ready": False,
        "creg_registered": False,
        "signal": None,
        "imsi": None,
        "iccid": None,
        "imei": None,
    }

    opened = False

    try:
        client.open()
        opened = True

        # Allow modem stack + SIM state to settle after opening
        time.sleep(2.0)

        if client._serial:
            client._serial.reset_input_buffer()
            client._serial.reset_output_buffer()

        try:
            resp = client._command_expect_ok(
                "AT",
                "AT_NOT_RESPONDING",
                time.monotonic() + command_timeout,
            )
            result["at_ok"] = "OK" in resp
        except Exception:
            return result

        try:
            client._command_expect_ok(
                "ATE0",
                "AT_NOT_RESPONDING",
                time.monotonic() + command_timeout,
            )
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

        return result

    finally:
        if opened:
            client.close()


def _build_groups():
    groups = {}

    for dev in glob.glob("/dev/serial/by-id/*if02*"):
        base = os.path.basename(dev)
        physical = base.split("-if")[0]
        groups.setdefault(physical, {})["if02"] = dev

    for dev in glob.glob("/dev/serial/by-id/*if03*"):
        base = os.path.basename(dev)
        physical = base.split("-if")[0]
        groups.setdefault(physical, {})["if03"] = dev

    return groups


def _select_id(probe):
    return probe.get("imsi") or probe.get("iccid") or probe.get("imei")


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0):
    modems = []
    groups = _build_groups()

    for physical, ifaces in groups.items():
        print(f"[GROUP] {physical} → {ifaces}")

        selected = None

        for iface in ("if02", "if03"):
            dev = ifaces.get(iface)
            if not dev:
                continue

            port = os.path.realpath(dev)

            probe = _probe_modem(
                port=port,
                device_id=dev,
                iface=iface,
                serial_timeout=serial_timeout,
                command_timeout=command_timeout,
            )

            print(f"[TRY] {port} ({iface}) → {probe}")

            if probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]:
                selected = probe
                print(f"[SELECTED] {port} ({iface}) ✅")
                break

        if not selected:
            continue

        sim_id = _select_id(selected)
        if not sim_id:
            continue

        modems.append(
            {
                "sim_id": str(sim_id),
                "device_id": selected["device_id"],
                "port": selected["port"],
                "interface": selected["interface"],
                "at_ok": selected["at_ok"],
                "sim_ready": selected["sim_ready"],
                "creg_registered": selected["creg_registered"],
                "signal": selected["signal"],
                "imsi": selected["imsi"],
                "iccid": selected["iccid"],
                "imei": selected["imei"],
            }
        )

    return modems