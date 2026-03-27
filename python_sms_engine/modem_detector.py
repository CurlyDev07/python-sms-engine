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
    data: Dict[str, Optional[str]] = {
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


def _read_sysfs_attr(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _sysfs_ttyusb_info(ttyusb_name: str) -> Optional[Tuple[str, int, str]]:
    """
    Resolves a ttyUSB device's physical USB parent and interface number via sysfs.

    /sys/class/tty/ttyUSB2 is a symlink resolving to something like:
      /sys/devices/pci.../usb1/1-2/1-2.1/1-2.1:1.2/ttyUSB2/tty/ttyUSB2

    The component "1-2.1:1.2" encodes:
      - "1-2.1"  = physical USB device (bus + port path) — unique per physical modem
      - "1"      = configuration number
      - "2"      = interface number (2 = if02, 3 = if03)

    Returns:
        (physical, interface_num, device_sysfs_path)
        e.g. ("1-2.1", 2, "/sys/devices/.../1-2.1")
    or None if the path cannot be parsed.
    """
    sysfs_link = f"/sys/class/tty/{ttyusb_name}"
    if not os.path.exists(sysfs_link):
        return None

    real_path = os.path.realpath(sysfs_link)

    # Match the USB interface path component: <physical>:<config>.<interface>/
    m = re.search(r"^(.+/)(\d[\d.-]*):(\d+)\.(\d+)/", real_path)
    if not m:
        return None

    # group(1) ends with the slash AFTER the physical device dir, e.g. ".../1-2/"
    # group(2) is the physical device dir name, e.g. "1-2.1"
    # So physical device sysfs path = group(1) stripped of trailing slash + "/" + group(2)
    # which is the same as: group(1) + group(2) would be WRONG (duplicates the dir name)
    # Correct: the physical device dir is group(1).rstrip("/") since group(1) already
    # ends at the slash that follows the physical device directory.
    #
    # Path breakdown:
    #   /sys/devices/.../1-2/    1-2.1/    1-2.1:1.2/usb_serial/ttyUSBX/tty/ttyUSBX
    #                  ^^^^^^^^  ^^^^^^    ^^^^^^^^^^
    #                  group(1)  group(2)  interface (not captured directly)
    #
    # group(1) = "/sys/devices/.../1-2/1-2.1/"  ← includes trailing slash
    # So the physical device sysfs dir = group(1) without its trailing slash
    device_sysfs = m.group(1).rstrip("/")    # e.g. /sys/devices/.../1-2/1-2.1
    physical = m.group(2)                    # e.g. "1-2.1"
    interface_num = int(m.group(4))          # e.g. 2

    return physical, interface_num, device_sysfs


def _build_sysfs_modem_groups() -> Dict[str, Dict[str, str]]:
    """
    Groups /dev/ttyUSB* devices by physical USB parent using sysfs.

    - Reads USB topology directly from /sys/class/tty — no /dev/serial/by-id dependency.
    - Filters to Quectel devices only (idVendor == 2c7c).
    - Retains only if02 and if03 per physical modem; ignores if00 and if01.
    - Deterministic: sorted by ttyUSB number, not hash or by-id name.

    For 5 physical Quectel modems this produces exactly 5 groups.

    Returns:
        {
            "1-2.1": {"if02": "/dev/ttyUSB2",  "if03": "/dev/ttyUSB3"},
            "1-2.2": {"if02": "/dev/ttyUSB6",  "if03": "/dev/ttyUSB7"},
            "1-2.3": {"if02": "/dev/ttyUSB10", "if03": "/dev/ttyUSB11"},
            ...
        }
    """
    groups: Dict[str, Dict[str, str]] = {}

    all_tty = sorted(glob.glob("/dev/ttyUSB*"), key=_parse_ttyusb_num)
    print(f"[SYSFS SCAN] found {len(all_tty)} ttyUSB devices: {all_tty}")

    for ttyusb_path in all_tty:
        ttyusb_name = os.path.basename(ttyusb_path)
        info = _sysfs_ttyusb_info(ttyusb_name)
        if info is None:
            print(f"[SYSFS SKIP] {ttyusb_name} -> could not parse sysfs path")
            continue

        physical, interface_num, device_sysfs = info

        vendor = _read_sysfs_attr(os.path.join(device_sysfs, "idVendor"))
        print(f"[SYSFS]  {ttyusb_name} -> physical={physical} if={interface_num} vendor={vendor} sysfs={device_sysfs}")

        if vendor != QUECTEL_VENDOR_ID:
            continue

        if interface_num == 2:
            groups.setdefault(physical, {})
            groups[physical]["if02"] = ttyusb_path
        elif interface_num == 3:
            groups.setdefault(physical, {})
            groups[physical]["if03"] = ttyusb_path

    print(f"[SYSFS GROUPS] {groups}")
    return groups


def _select_sim_id(item: Dict) -> Optional[str]:
    return item.get("imsi") or item.get("iccid") or item.get("imei")


def detect_modems(serial_timeout: float = 3.0, command_timeout: float = 5.0) -> List[Dict]:
    """
    Discovers all ready Quectel modems via sysfs USB topology.

    For each physical modem:
      1. Probe if02 first.
      2. Fall back to if03 only if if02 is not SIM-ready.
      3. Skip if00 and if01 entirely.

    Normal case: N modems = N probes.
    Worst case: N modems = 2N probes.
    """
    groups = _build_sysfs_modem_groups()
    modems: List[Dict] = []

    for physical_modem, ports in sorted(groups.items()):
        print(f"[USB MODEM] {physical_modem} -> {ports}")

        primary_port = ports.get("if02")
        fallback_port = ports.get("if03")

        best_probe = None
        best_interface = None

        # Probe if02 first
        if primary_port and os.path.exists(primary_port):
            probe = _probe_port(primary_port, serial_timeout, command_timeout)
            print(f"[TRY] {physical_modem} if02 -> score={probe['score']} sim_ready={probe['sim_ready']} creg={probe['creg_registered']}")

            if probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]:
                best_probe = probe
                best_interface = "if02"

        # Only try if03 when if02 failed
        if best_probe is None and fallback_port and os.path.exists(fallback_port):
            probe = _probe_port(fallback_port, serial_timeout, command_timeout)
            print(f"[TRY] {physical_modem} if03 -> score={probe['score']} sim_ready={probe['sim_ready']} creg={probe['creg_registered']}")

            if probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]:
                best_probe = probe
                best_interface = "if03"

        if best_probe is None:
            print(f"[SKIP] {physical_modem} -> no usable interface found")
            continue

        sim_id = _select_sim_id(best_probe)
        if not sim_id:
            print(f"[SKIP] {physical_modem} -> no identity (IMSI/ICCID/IMEI)")
            continue

        # Store fallback_port for sms_service to use during send failures.
        # If we're already on if03 (if02 failed probing), there is no further fallback.
        stored_fallback = fallback_port if best_interface == "if02" else None

        modems.append(
            {
                "sim_id": str(sim_id),
                "device_id": physical_modem,
                "port": best_probe["port"],
                "fallback_port": stored_fallback,
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

        print(f"[SELECTED] {physical_modem} -> {best_interface} -> {best_probe['port']} sim_id={sim_id}")

    return modems
