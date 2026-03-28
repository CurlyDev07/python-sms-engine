import glob
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from at_client import ModemATClient

QUECTEL_VENDOR_ID = "2c7c"


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


def _get_identity(
    client: ModemATClient, timeout: float = 2.0
) -> Dict[str, Optional[str]]:
    data: Dict[str, Optional[str]] = {"imsi": None, "iccid": None, "imei": None}

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

    result: Dict = {
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
        if result["at_ok"]:        score += 1
        if result["imei"]:         score += 2
        if result["sim_ready"]:    score += 4
        if result["creg_registered"]: score += 4
        if result["imsi"]:         score += 3
        if result["iccid"]:        score += 3
        result["score"] = score

        return result

    finally:
        if opened:
            client.close()


def _read_sysfs_attr(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _sysfs_ttyusb_info(ttyusb_name: str) -> Optional[Tuple[str, int, str]]:
    """
    Resolves a ttyUSB device's physical USB parent and interface number via sysfs.

    /sys/class/tty/ttyUSBX symlink resolves to a path containing:
        .../<physical>:<config>.<interface>/...
    e.g.  .../3-7.4.4:1.2/...  →  physical="3-7.4.4", interface=2

    Returns (physical, interface_num, device_sysfs_path) or None.
    """
    sysfs_link = f"/sys/class/tty/{ttyusb_name}"
    if not os.path.exists(sysfs_link):
        return None

    real_path = os.path.realpath(sysfs_link)
    m = re.search(r"^(.+/)(\d[\d.-]*):(\d+)\.(\d+)/", real_path)
    if not m:
        return None

    # group(1) ends with "/" after the physical device dir, e.g. ".../3-7.4/"
    # group(2) is the physical device component, e.g. "3-7.4.4"
    # So device_sysfs = group(1) stripped of its trailing slash
    device_sysfs = m.group(1).rstrip("/")
    physical = m.group(2)
    interface_num = int(m.group(4))

    return physical, interface_num, device_sysfs


def _collect_if02_ports() -> List[Tuple[str, str, Optional[str]]]:
    """
    Single-pass sysfs scan. Collects only Quectel if=2 ports as primaries,
    and their if=3 siblings as fallback (derived, never probed at startup).

    Returns list of:
        (physical_parent, if02_port, if03_port_or_None)

    For 5 physical modems → exactly 5 entries.
    No probe I/O at this stage — pure sysfs reads.
    """
    if02: Dict[str, str] = {}
    if03: Dict[str, str] = {}

    all_tty = sorted(glob.glob("/dev/ttyUSB*"), key=_parse_ttyusb_num)
    print(f"[SYSFS SCAN] {len(all_tty)} ttyUSB devices found")

    for ttyusb_path in all_tty:
        ttyusb_name = os.path.basename(ttyusb_path)
        info = _sysfs_ttyusb_info(ttyusb_name)
        if info is None:
            continue

        physical, interface_num, device_sysfs = info
        vendor = _read_sysfs_attr(os.path.join(device_sysfs, "idVendor"))

        if vendor != QUECTEL_VENDOR_ID:
            continue

        if interface_num == 2:
            if02[physical] = ttyusb_path
            print(f"[SYSFS] {ttyusb_name} -> {physical} if02 (primary)")
        elif interface_num == 3:
            if03[physical] = ttyusb_path
            print(f"[SYSFS] {ttyusb_name} -> {physical} if03 (fallback, not probed)")

    result = [
        (physical, port, if03.get(physical))
        for physical, port in sorted(if02.items())
    ]
    print(f"[SYSFS] {len(result)} modem groups: { {p: (p2, f) for p, p2, f in result} }")
    return result


def _select_sim_id(item: Dict) -> Optional[str]:
    return item.get("imsi") or item.get("iccid") or item.get("imei")


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict]:
    """
    Discovers ready Quectel modems via sysfs USB topology.

    Strategy:
      - Enumerate only if=2 ports (one per physical modem).
      - Probe only if=2. if=3 is stored as fallback_port, never probed here.
      - A modem is accepted only when: at_ok + sim_ready + creg_registered.
      - sim_id  = IMSI (preferred) → ICCID → IMEI
      - modem_id = IMEI (hardware identity, separate from SIM identity)

    N modems = exactly N probes (worst case). No if=3 probing at startup.
    """
    entries = _collect_if02_ports()
    modems: List[Dict] = []

    for physical, primary_port, fallback_port in entries:
        print(f"[PROBE] {physical} -> {primary_port} (fallback: {fallback_port})")

        if not os.path.exists(primary_port):
            print(f"[SKIP] {physical} -> port gone")
            continue

        probe = _probe_port(primary_port, serial_timeout, command_timeout)
        print(
            f"[TRY] {physical} if02={primary_port} "
            f"score={probe['score']} sim_ready={probe['sim_ready']} creg={probe['creg_registered']}"
        )

        if not (probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]):
            print(f"[SKIP] {physical} -> if02 not ready (sim_ready={probe['sim_ready']} creg={probe['creg_registered']})")
            continue

        sim_id = _select_sim_id(probe)
        if not sim_id:
            print(f"[SKIP] {physical} -> no SIM identity (IMSI/ICCID/IMEI all missing)")
            continue

        modems.append(
            {
                "sim_id":       str(sim_id),
                "modem_id":     probe.get("imei"),   # IMEI = hardware identity
                "device_id":    physical,
                "port":         primary_port,
                "fallback_port": fallback_port,       # if03, stored but not probed
                "interface":    "if02",
                "at_ok":        probe["at_ok"],
                "sim_ready":    probe["sim_ready"],
                "creg_registered": probe["creg_registered"],
                "signal":       probe["signal"],
                "imsi":         probe["imsi"],
                "iccid":        probe["iccid"],
                "imei":         probe["imei"],
            }
        )
        print(
            f"[SELECTED] {physical} -> {primary_port} "
            f"sim_id={sim_id} modem_id={probe.get('imei')}"
        )

    return modems
