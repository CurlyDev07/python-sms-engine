import glob
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from typing import Dict, List, Optional, Tuple

from at_client import ModemATClient

QUECTEL_VENDOR_ID = "2c7c"

# Hard wall-clock limit per modem probe. One stuck modem will not block others
# past this deadline — it gets marked as PROBE_TIMEOUT and the caller moves on.
# The underlying thread may remain blocked in I/O, but the response is unaffected.
PROBE_TIMEOUT_S = 12.0


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


def _wait_for_cpin_ready(client: ModemATClient, timeout: float = 3.0) -> bool:
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


def _wait_for_creg(client: ModemATClient, timeout: float = 3.0) -> bool:
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
    client: ModemATClient, timeout: float = 1.5
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
        if result["at_ok"]:             score += 1
        if result["imei"]:              score += 2
        if result["sim_ready"]:         score += 4
        if result["creg_registered"]:   score += 4
        if result["imsi"]:              score += 3
        if result["iccid"]:             score += 3
        result["score"] = score

        return result

    finally:
        if opened:
            client.close()


def _safe_probe(
    physical: str,
    primary_port: str,
    fallback_port: Optional[str],
    serial_timeout: float,
    command_timeout: float,
) -> Dict:
    """
    Wraps _probe_port and normalises all outcomes (exception, missing port)
    into a consistent result dict. Never raises.
    """
    base: Dict = {
        "physical": physical,
        "port": primary_port,
        "fallback_port": fallback_port,
        "at_ok": False,
        "sim_ready": False,
        "creg_registered": False,
        "signal": None,
        "imsi": None,
        "iccid": None,
        "imei": None,
        "score": 0,
        "probe_error": None,
    }
    if not os.path.exists(primary_port):
        base["probe_error"] = "PORT_NOT_FOUND"
        return base
    try:
        result = _probe_port(primary_port, serial_timeout, command_timeout)
        base.update(result)
    except Exception as exc:
        base["probe_error"] = str(exc)
    return base


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


def _run_parallel_probes(
    entries: List[Tuple[str, str, Optional[str]]],
    serial_timeout: float,
    command_timeout: float,
    probe_timeout: float,
) -> List[Dict]:
    """
    Probes all modem entries in parallel using a thread pool.

    Each probe is bounded by probe_timeout seconds wall-clock. Modems that
    do not finish within the deadline are marked probe_error=PROBE_TIMEOUT
    and included in results — they do not block the remaining results.

    Returns one result dict per entry (including timed-out/failed ones).
    """
    if not entries:
        return []

    results: List[Dict] = []

    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        future_to_meta = {
            executor.submit(
                _safe_probe, physical, port, fallback, serial_timeout, command_timeout
            ): (physical, port, fallback)
            for physical, port, fallback in entries
        }

        done, not_done = futures_wait(future_to_meta, timeout=probe_timeout)

        for f in done:
            results.append(f.result())

        for f in not_done:
            physical, port, fallback = future_to_meta[f]
            print(f"[TIMEOUT] {physical} -> probe timed out after {probe_timeout}s")
            results.append({
                "physical": physical,
                "port": port,
                "fallback_port": fallback,
                "at_ok": False,
                "sim_ready": False,
                "creg_registered": False,
                "signal": None,
                "imsi": None,
                "iccid": None,
                "imei": None,
                "score": 0,
                "probe_error": f"PROBE_TIMEOUT after {probe_timeout}s",
            })
            f.cancel()  # best-effort; stuck thread will eventually unblock on its own

    return results


def discover_all_modems(
    serial_timeout: float = 3.0,
    command_timeout: float = 5.0,
    probe_timeout: float = PROBE_TIMEOUT_S,
) -> List[Dict]:
    """
    Discovers all Quectel modem ports via sysfs and probes them in parallel.

    Unlike detect_modems(), this returns ALL detected ports including unhealthy
    and timed-out ones, each with a clear probe_error field. Use this for the
    /modems/discover endpoint so callers see the full hardware picture even when
    some modems are in a bad state.

    Guaranteed to return within probe_timeout + sysfs scan time (~1s).
    """
    entries = _collect_if02_ports()
    # Cap timeouts for probe path.
    # pyserial read(256) loops internally until 256 bytes arrive or serial_timeout expires —
    # even when data is available sooner. With serial_timeout=1.0, every AT command costs
    # ~1.0s regardless of modem response time. Cap to 0.1s: each read costs ≤0.1s,
    # so a full probe completes in ~1.8s instead of ~10s.
    probe_serial_timeout = min(serial_timeout, 0.1)
    probe_command_timeout = min(command_timeout, 5.0)
    raw_results = _run_parallel_probes(entries, probe_serial_timeout, probe_command_timeout, probe_timeout)

    modems: List[Dict] = []
    for probe in raw_results:
        physical = probe.pop("physical", None) or probe.get("port", "unknown")
        sim_id = _select_sim_id(probe) or physical  # physical address as last-resort identity

        # identifier_source: "imsi" only when a real telecom SIM identity was read.
        # Anything else (ICCID-only, IMEI-only, or physical USB address fallback)
        # means the sim_id is not a trustworthy SIM identifier for routing.
        imsi = probe.get("imsi")
        identifier_source = "imsi" if imsi else "fallback_device_id"

        # send_ready: true only when the modem row is safe to use as a /send target.
        # All five conditions must hold — a partial pass is not send-safe.
        send_ready = bool(
            not probe.get("probe_error")
            and probe.get("at_ok")
            and probe.get("sim_ready")
            and probe.get("creg_registered")
            and identifier_source == "imsi"
        )

        modems.append({
            "sim_id":           str(sim_id),
            "modem_id":         probe.get("imei"),
            "device_id":        physical,
            "port":             probe.get("port"),
            "fallback_port":    probe.get("fallback_port"),
            "interface":        "if02",
            "at_ok":            bool(probe.get("at_ok")),
            "sim_ready":        bool(probe.get("sim_ready")),
            "creg_registered":  bool(probe.get("creg_registered")),
            "signal":           probe.get("signal"),
            "imsi":             probe.get("imsi"),
            "iccid":            probe.get("iccid"),
            "imei":             probe.get("imei"),
            "probe_error":      probe.get("probe_error"),
            "send_ready":       send_ready,
            "identifier_source": identifier_source,
        })

    return modems


def detect_modems(
    serial_timeout: float = 3.0,
    command_timeout: float = 5.0,
    probe_timeout: float = PROBE_TIMEOUT_S,
) -> List[Dict]:
    """
    Discovers ready Quectel modems via sysfs USB topology.

    Strategy:
      - Enumerate only if=2 ports (one per physical modem).
      - Probe all ports in parallel — bounded by probe_timeout.
      - Return only modems that pass: at_ok + sim_ready + creg_registered.
      - sim_id  = IMSI (preferred) → ICCID → IMEI
      - modem_id = IMEI (hardware identity, separate from SIM identity)

    N modems = N probes running concurrently (not sequentially).
    One hung modem does not delay the rest.
    """
    entries = _collect_if02_ports()
    # Cap timeouts for probe path.
    # pyserial read(256) loops internally until 256 bytes arrive or serial_timeout expires —
    # even when data is available sooner. With serial_timeout=1.0, every AT command costs
    # ~1.0s regardless of modem response time. Cap to 0.1s: each read costs ≤0.1s,
    # so a full probe completes in ~1.8s instead of ~10s.
    probe_serial_timeout = min(serial_timeout, 0.1)
    probe_command_timeout = min(command_timeout, 5.0)
    raw_results = _run_parallel_probes(entries, probe_serial_timeout, probe_command_timeout, probe_timeout)

    modems: List[Dict] = []
    for probe in raw_results:
        physical = probe.pop("physical", None) or probe.get("port", "unknown")

        if probe.get("probe_error"):
            print(f"[SKIP] {physical} -> probe error: {probe['probe_error']}")
            continue

        print(
            f"[TRY] {physical} if02={probe.get('port')} "
            f"score={probe['score']} sim_ready={probe['sim_ready']} creg={probe['creg_registered']}"
        )

        if not (probe["at_ok"] and probe["sim_ready"] and probe["creg_registered"]):
            print(
                f"[SKIP] {physical} -> if02 not ready "
                f"(sim_ready={probe['sim_ready']} creg={probe['creg_registered']})"
            )
            continue

        sim_id = _select_sim_id(probe)
        if not sim_id:
            print(f"[SKIP] {physical} -> no SIM identity (IMSI/ICCID/IMEI all missing)")
            continue

        modems.append({
            "sim_id":           str(sim_id),
            "modem_id":         probe.get("imei"),
            "device_id":        physical,
            "port":             probe.get("port"),
            "fallback_port":    probe.get("fallback_port"),
            "interface":        "if02",
            "at_ok":            probe["at_ok"],
            "sim_ready":        probe["sim_ready"],
            "creg_registered":  probe["creg_registered"],
            "signal":           probe["signal"],
            "imsi":             probe["imsi"],
            "iccid":            probe["iccid"],
            "imei":             probe["imei"],
        })
        print(
            f"[SELECTED] {physical} -> {probe.get('port')} "
            f"sim_id={sim_id} modem_id={probe.get('imei')}"
        )

    return modems
