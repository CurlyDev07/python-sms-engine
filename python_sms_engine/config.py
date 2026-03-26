import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple


class Settings:
    def __init__(self) -> None:
        self.sim_map_file = os.getenv("SMS_ENGINE_SIM_MAP_FILE", "sim_map.json")
        self.serial_timeout = float(os.getenv("SMS_ENGINE_SERIAL_TIMEOUT", "3"))
        self.command_timeout = float(os.getenv("SMS_ENGINE_COMMAND_TIMEOUT", "10"))
        self.send_timeout = float(os.getenv("SMS_ENGINE_SEND_TIMEOUT", "30"))
        self.host = os.getenv("SMS_ENGINE_HOST", "0.0.0.0")
        self.port = int(os.getenv("SMS_ENGINE_PORT", "8000"))

settings = Settings()

def load_sim_map(path: str) -> Dict[int, str]:
    map_file = Path(path)
    if not map_file.exists():
        raise RuntimeError(f"SIM map file not found: {map_file}")

    try:
        raw = json.loads(map_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SIM map JSON is invalid: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError("SIM map must be a JSON object")

    sim_map: Dict[int, str] = {}
    for sim_id, port in raw.items():
        if not isinstance(port, str) or not port.strip():
            raise RuntimeError(f"SIM map value for {sim_id} must be a non-empty string")
        try:
            sim_map[int(sim_id)] = port.strip()
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"SIM id '{sim_id}' must be an integer-like key") from exc

    return sim_map


def load_sim_map_safe(path: str) -> Tuple[Dict[int, str], Optional[str]]:
    try:
        return load_sim_map(path), None
    except RuntimeError as exc:
        return {}, str(exc)
