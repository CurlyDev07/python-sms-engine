import glob
import serial
import time

def send_at(port, command="AT", timeout=2):
    try:
        ser = serial.Serial(port, baudrate=115200, timeout=timeout)
        time.sleep(0.5)

        ser.write((command + "\r").encode())
        time.sleep(0.5)

        response = ser.read_all().decode(errors="ignore")
        ser.close()

        return response
    except Exception as e:
        return None


def detect_modems():
    devices = glob.glob("/dev/serial/by-id/*")

    modems = []

    for dev in devices:
        # we only test IF02 (AT port usually)
        if "if02" not in dev:
            continue

        response = send_at(dev)

        if response and "OK" in response:
            sim_status = send_at(dev, "AT+CPIN?")
            signal = send_at(dev, "AT+CSQ")

            modems.append({
                "device_id": dev,
                "port": dev,
                "at_ok": True,
                "sim_ready": "READY" in (sim_status or ""),
                "signal": signal.strip() if signal else None,
            })
        else:
            modems.append({
                "device_id": dev,
                "port": dev,
                "at_ok": False,
                "sim_ready": False,
                "signal": None,
            })

    return modems
