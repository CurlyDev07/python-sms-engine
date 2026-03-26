import glob
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from at_client import ModemATClient


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


def _query_ok_response(
    client: ModemATClient,
    command: str,
    timeout_code: str,
    command_timeout: float,
) -> str:
    return client._command_expect_ok(
        command,
        timeout_code,
        deadline=time.monotonic() + command_timeout,
        retries=0,
    )


def _query_cpms(client: ModemATClient, command_timeout: float) -> Tuple[bool, Optional[str]]:
    try:
        resp = _query_ok_response(
            client,
            "AT+CPMS?",
            "AT_NOT_RESPONDING",
            command_timeout,
        )
        return "+CPMS:" in resp, resp
    except Exception:
        return False, None


def _query_identity(client: ModemATClient, command_timeout: float) -> Dict[str, Optional[str]]:
    identity: Dict[str, Optional[str]] = {
        "imsi": None,
        "iccid": None,
        "imei": None,
        "msisdn": None,
    }

    try:
        resp = _query_ok_response(client, "AT+CIMI", "AT_NOT_RESPONDING", command_timeout)
        identity["imsi"] = _extract_first_meaningful_line(resp)
    except Exception:
        pass

    try:
        resp = _query_ok_response(client, "AT+CCID", "AT_NOT_RESPONDING", command_timeout)
        line = _extract_first_meaningful_line(resp)
        if line:
            identity["iccid"] = line.replace("+CCID:", "").strip()
    except Exception:
        pass

    try:
        resp = _query_ok_response(client, "AT+GSN", "AT_NOT_RESPONDING", command_timeout)
        identity["imei"] = _extract_first_meaningful_line(resp)
    except Exception:
        pass

    try:
        resp = _query_ok_response(client, "AT+CNUM", "AT_NOT_RESPONDING", command_timeout)
        identity["msisdn"] = _extract_first_meaningful_line(resp)
    except Exception:
        pass

    return identity


def _probe_modem(
    port: str,
    device_id: str,
    interface_name: str,
    serial_timeout: float,
    command_timeout: float,
) -> Dict[str, Any]:
    client = ModemATClient(
        port=port,
        serial_timeout=serial_timeout,
        command_timeout=command_timeout,
    )

    result: Dict[str, Any] = {
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

        try:
            client._write(b"\r\r\r", timeout_code="AT_NOT_RESPONDING")
            time.sleep(0.15)
            if client._serial:
                client._serial.reset_input_buffer()
        except Exception:
            pass

        try:
            at_resp = _query_ok_response(client, "AT", "AT_NOT_RESPONDING", command_timeout)
            result["at_ok"] = "OK" in at_resp
        except Exception:
            result["at_ok"] = False

        if not result["at_ok"]:
            return result

        try:
            _query_ok_response(client, "ATE0", "AT_NOT_RESPONDING", command_timeout)
        except Exception:
            pass

        cpms_ok, cpms_resp = _query_cpms(client, command_timeout)
        result["sms_capable"] = cpms_ok
        result["cpms"] = cpms_resp

        try:
            cpin = _query_ok_response(client, "AT+CPIN?", "AT_NOT_RESPONDING", command_timeout)
            result["sim_ready"] = "READY" in cpin
        except Exception:
            result["sim_ready"] = False

        try:
            creg = _query_ok_response(client, "AT+CREG?", "AT_NOT_RESPONDING", command_timeout)
            result["creg_registered"] = _is_registered(creg)
        except Exception:
            result["creg_registered"] = False

        try:
            client._write(b"AT+CSQ\r", timeout_code="AT_NOT_RESPONDING")
            csq = client._read_until(
                expected=["+CSQ:", "OK"],
                failure=["ERROR", "+CME ERROR", "+CMS ERROR"],
                timeout=command_timeout,
                timeout_code="AT_NOT_RESPONDING",
            )
            result["signal"] = _extract_signal(csq)
            result["signal_ok"] = _is_usable_signal(csq)
        except Exception:
            result["signal"] = None
            result["signal_ok"] = False

        identity = _query_identity(client, command_timeout)
        result["imsi"] = identity["imsi"]
        result["iccid"] = identity["iccid"]
        result["imei"] = identity["imei"]
        result["msisdn"] = identity["msisdn"]

        return result

    finally:
        if opened:
            client.close()


def _build_candidate_groups() -> Dict[str, Dict[str, str]]:
    groups: Dict[str, Dict[str, str]] = {}

    for dev in sorted(glob.glob("/dev/serial/by-id/*if02*")):
        base = os.path.basename(dev)
        physical = base.split("-if", 1)[0]
        groups.setdefault(physical, {})
        groups[physical]["if02"] = dev

    for dev in sorted(glob.glob("/dev/serial/by-id/*if03*")):
        base = os.path.basename(dev)
        physical = base.split("-if", 1)[0]
        groups.setdefault(physical, {})
        groups[physical]["if03"] = dev

    return groups


def _select_sim_id(probe: Dict[str, Any]) -> Optional[str]:
    if probe.get("imsi"):
        return str(probe["imsi"])
    if probe.get("iccid"):
        return str(probe["iccid"])
    if probe.get("imei"):
        return str(probe["imei"])
    return None


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict[str, Any]]:
    modems: List[Dict[str, Any]] = []
    groups = _build_candidate_groups()

    for physical_id, interfaces in sorted(groups.items()):
        print(f"[GROUP] {physical_id} → {interfaces}")

        selected_probe: Optional[Dict[str, Any]] = None

        for interface_name in ("if02", "if03"):
            dev = interfaces.get(interface_name)
            if not dev:
                continue

            try:
                port = os.path.realpath(dev)

                if not port.startswith("/dev/ttyUSB"):
                    print(f"[SKIP] {dev} → not ttyUSB")
                    continue

                if not os.path.exists(port):
                    print(f"[SKIP] {dev} → port missing")
                    continue

                probe = _probe_modem(
                    port=port,
                    device_id=dev,
                    interface_name=interface_name,
                    serial_timeout=serial_timeout,
                    command_timeout=command_timeout,
                )

                print(f"[TRY] {port} ({interface_name}) → {probe}")

                # Primary rule: if02 with AT + SIM READY + REGISTERED
                if (
                    interface_name == "if02"
                    and probe["at_ok"]
                    and probe["sim_ready"]
                    and probe["creg_registered"]
                ):
                    selected_probe = probe
                    print(f"[SELECTED] {port} ({interface_name}) ✅")
                    break

                # Fallback rule: if03 only if it also looks usable
                if (
                    interface_name == "if03"
                    and probe["at_ok"]
                    and probe["sim_ready"]
                    and probe["creg_registered"]
                ):
                    selected_probe = probe
                    print(f"[SELECTED-FALLBACK] {port} ({interface_name}) ✅")
                    break

            except Exception as exc:
                print(f"[ERROR] {dev} → {str(exc)}")
                continue

        if not selected_probe:
            continue

        sim_id = _select_sim_id(selected_probe)
        if not sim_id:
            print(f"[SKIP] {physical_id} → no stable identity")
            continue

        modems.append(
            {
                "sim_id": sim_id,
                "device_id": selected_probe["device_id"],
                "port": selected_probe["port"],
                "interface": selected_probe["interface"],
                "at_ok": selected_probe["at_ok"],
                "sms_capable": selected_probe["sms_capable"],  # debug only now
                "sim_ready": selected_probe["sim_ready"],
                "creg_registered": selected_probe["creg_registered"],
                "signal": selected_probe["signal"],
                "signal_ok": selected_probe["signal_ok"],
                "imsi": selected_probe["imsi"],
                "iccid": selected_probe["iccid"],
                "imei": selected_probe["imei"],
                "msisdn": selected_probe["msisdn"],
                "cpms": selected_probe["cpms"],
            }
        )

    return modems