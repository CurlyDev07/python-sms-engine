import glob
import os
import time
from typing import Dict, List, Optional

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


def _parse_csq_value(raw: str) -> Optional[int]:
    if not raw:
        return None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("+CSQ:"):
            try:
                payload = s.split(":", 1)[1].strip()
                return int(payload.split(",", 1)[0].strip())
            except Exception:
                return None
    return None


def _is_registered(raw: str) -> bool:
    if not raw:
        return False
    compact = raw.replace(" ", "")
    return "+CREG:0,1" in compact or "+CREG:0,5" in compact


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
            compact = resp.replace(" ", "").upper()
            if "+CPIN:READY" in compact:
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
        "signal_value": None,
        "imsi": None,
        "iccid": None,
        "imei": None,
        "score": 0,
    }

    opened = False

    try:
        client.open()
        opened = True

        time.sleep(0.8)

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
            result["signal_value"] = _parse_csq_value(csq)
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


def _select_sim_id(item: Dict) -> Optional[str]:
    return item.get("imsi") or item.get("iccid") or item.get("imei")


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict]:
    ports = sorted(glob.glob("/dev/ttyUSB*"))
    print(f"[SCAN PORTS] {ports}")

    all_results: List[Dict] = []

    for port in ports:
        try:
            probe = _probe_port(port, serial_timeout, command_timeout)
            print(f"[TRY] {port} → {probe}")
            all_results.append(probe)
        except Exception as exc:
            print(f"[ERROR] {port} → {exc}")

    # keep only usable modem-ish ports
    usable = [
        r for r in all_results
        if r["at_ok"] and r["imei"]
    ]

    # group by imei
    by_imei: Dict[str, List[Dict]] = {}
    for item in usable:
        by_imei.setdefault(item["imei"], []).append(item)

    selected_modems: List[Dict] = []

    for imei, items in by_imei.items():
        # choose highest score, then lower ttyUSB number
        items = sorted(
            items,
            key=lambda x: (
                -x["score"],
                int(x["port"].replace("/dev/ttyUSB", "")) if x["port"].startswith("/dev/ttyUSB") else 9999
            )
        )

        best = items[0]

        # require actual usable modem
        if not (best["sim_ready"] and best["creg_registered"]):
            continue

        sim_id = _select_sim_id(best)
        if not sim_id:
            continue

        selected_modems.append(
            {
                "sim_id": str(sim_id),
                "device_id": best["port"],
                "port": best["port"],
                "interface": None,
                "at_ok": best["at_ok"],
                "sim_ready": best["sim_ready"],
                "creg_registered": best["creg_registered"],
                "signal": best["signal"],
                "imsi": best["imsi"],
                "iccid": best["iccid"],
                "imei": best["imei"],
            }
        )

        print(f"[SELECTED] IMEI={imei} PORT={best['port']} SCORE={best['score']}")

    return selected_modems