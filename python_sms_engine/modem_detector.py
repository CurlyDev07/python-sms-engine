import glob
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from at_client import ModemATClient


# -------------------------------
# Helpers
# -------------------------------

def _extract_signal(raw: str) -> Optional[str]:
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("+CSQ:"):
            return stripped
    return None


def _extract_first_meaningful_line(raw: str) -> Optional[str]:
    if not raw:
        return None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"OK", "ERROR"}:
            continue
        if stripped.startswith("AT"):
            continue
        if stripped.startswith("+CME ERROR") or stripped.startswith("+CMS ERROR"):
            continue
        return stripped

    return None


def _is_registered(creg_response: str) -> bool:
    if not creg_response:
        return False

    normalized = creg_response.replace(" ", "")
    return "+CREG:0,1" in normalized or "+CREG:0,5" in normalized


def _parse_csq_value(raw: str) -> Optional[int]:
    if not raw:
        return None

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("+CSQ:"):
            try:
                payload = stripped.split(":", 1)[1].strip()
                rssi_text = payload.split(",", 1)[0].strip()
                return int(rssi_text)
            except Exception:
                return None

    return None


def _is_usable_signal(raw: str) -> bool:
    value = _parse_csq_value(raw)
    if value is None:
        return False
    if value == 99:
        return False
    return value >= 5


# -------------------------------
# AT wrappers
# -------------------------------

def _query_ok_response(client, command, timeout):
    return client._command_expect_ok(
        command,
        "AT_NOT_RESPONDING",
        deadline=time.monotonic() + timeout,
        retries=0,
    )


def _query_cpms(client, timeout) -> Tuple[bool, Optional[str]]:
    try:
        resp = _query_ok_response(client, "AT+CPMS?", timeout)
        return "+CPMS:" in resp, resp
    except:
        return False, None


def _wait_for_sim_ready(client, timeout=6) -> bool:
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            resp = _query_ok_response(client, "AT+CPIN?", 2)
            if "READY" in resp:
                return True
        except:
            pass

        time.sleep(0.5)

    return False


def _query_identity(client, timeout) -> Dict[str, Optional[str]]:
    identity = {
        "imsi": None,
        "iccid": None,
        "imei": None,
        "msisdn": None,
    }

    try:
        identity["imsi"] = _extract_first_meaningful_line(
            _query_ok_response(client, "AT+CIMI", timeout)
        )
    except:
        pass

    try:
        resp = _query_ok_response(client, "AT+CCID", timeout)
        line = _extract_first_meaningful_line(resp)
        if line:
            identity["iccid"] = line.replace("+CCID:", "").strip()
    except:
        pass

    try:
        identity["imei"] = _extract_first_meaningful_line(
            _query_ok_response(client, "AT+GSN", timeout)
        )
    except:
        pass

    try:
        identity["msisdn"] = _extract_first_meaningful_line(
            _query_ok_response(client, "AT+CNUM", timeout)
        )
    except:
        pass

    return identity


# -------------------------------
# Probe modem
# -------------------------------

def _probe_modem(port, device_id, interface_name, serial_timeout, command_timeout):
    client = ModemATClient(
        port=port,
        serial_timeout=serial_timeout,
        command_timeout=command_timeout,
    )

    result = {
        "device_id": device_id,
        "port": port,
        "interface": interface_name,
        "at_ok": False,
        "sms_capable": False,
        "sim_ready": False,
        "creg_registered": False,
        "signal": None,
        "signal_ok": False,
        "cpms": None,
        "imsi": None,
        "iccid": None,
        "imei": None,
        "msisdn": None,
    }

    opened = False

    try:
        client.open()
        opened = True

        # 🔥 FIX: WAIT FOR MODEM READY
        time.sleep(0.8)

        if client._serial:
            client._serial.reset_input_buffer()

        # AT
        try:
            at_resp = _query_ok_response(client, "AT", command_timeout)
            result["at_ok"] = "OK" in at_resp
        except:
            return result

        # Disable echo
        try:
            _query_ok_response(client, "ATE0", command_timeout)
        except:
            pass

        # CPMS (debug only)
        cpms_ok, cpms_resp = _query_cpms(client, command_timeout)
        result["sms_capable"] = cpms_ok
        result["cpms"] = cpms_resp

        # 🔥 FIX: WAIT FOR SIM READY
        result["sim_ready"] = _wait_for_sim_ready(client)

        # CREG
        try:
            creg = _query_ok_response(client, "AT+CREG?", command_timeout)
            result["creg_registered"] = _is_registered(creg)
        except:
            pass

        # CSQ
        try:
            client._write(b"AT+CSQ\r", timeout_code="AT_NOT_RESPONDING")
            csq = client._read_until(
                expected=["+CSQ:", "OK"],
                failure=["ERROR"],
                timeout=command_timeout,
                timeout_code="AT_NOT_RESPONDING",
            )
            result["signal"] = _extract_signal(csq)
            result["signal_ok"] = _is_usable_signal(csq)
        except:
            pass

        # Identity
        identity = _query_identity(client, command_timeout)
        result.update(identity)

        return result

    finally:
        if opened:
            client.close()


# -------------------------------
# Build groups
# -------------------------------

def _build_candidate_groups():
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


def _select_sim_id(probe):
    return probe.get("imsi") or probe.get("iccid") or probe.get("imei")


# -------------------------------
# MAIN DETECT
# -------------------------------

def detect_modems(serial_timeout=3.0, command_timeout=5.0):
    modems = []
    groups = _build_candidate_groups()

    for physical_id, interfaces in groups.items():
        print(f"[GROUP] {physical_id} → {interfaces}")

        selected = None

        for iface in ("if02", "if03"):
            dev = interfaces.get(iface)
            if not dev:
                continue

            port = os.path.realpath(dev)

            probe = _probe_modem(
                port,
                dev,
                iface,
                serial_timeout,
                command_timeout,
            )

            print(f"[TRY] {port} ({iface}) → {probe}")

            if (
                probe["at_ok"]
                and probe["sim_ready"]
                and probe["creg_registered"]
            ):
                selected = probe
                print(f"[SELECTED] {port} ({iface}) ✅")
                break

        if not selected:
            continue

        sim_id = _select_sim_id(selected)
        if not sim_id:
            continue

        modems.append({
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
        })

    return modems