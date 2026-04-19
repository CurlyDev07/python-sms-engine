from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class SendRequest(BaseModel):
    sim_id: str
    phone: str
    message: str
    meta: Optional[Dict[str, Any]] = None


class SendResponse(BaseModel):
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    success: bool
    service: str
    status: str


class ModemHealthItem(BaseModel):
    sim_id: Optional[str] = None
    modem_id: Optional[str] = None
    port: Optional[str] = None
    alive: bool
    last_ping_at: Optional[str] = None
    last_ping_ok: bool
    consecutive_failures: int = 0
    send_ready: bool


class ModemsHealthResponse(BaseModel):
    success: bool
    modems: List[ModemHealthItem]


class ModemDiscoverItem(BaseModel):
    sim_id: str
    modem_id: Optional[str] = None
    device_id: Optional[str] = None
    port: Optional[str] = None
    fallback_port: Optional[str] = None
    interface: Optional[str] = None
    at_ok: bool
    sim_ready: bool
    creg_registered: bool
    signal: Optional[str] = None
    imsi: Optional[str] = None
    iccid: Optional[str] = None
    imei: Optional[str] = None
    probe_error: Optional[str] = None

    # Send-readiness and identity source.
    # send_ready       = realtime single-probe result (strict).
    # effective_send_ready = smoothed result — only downgrades after 3 consecutive failures.
    # Use effective_send_ready for UI/operator display.
    # Use send_ready for strict hardware-state audit only.
    send_ready: bool = False
    identifier_source: str = "fallback_device_id"

    # Stability fields — for UI hysteresis and operator diagnostics.
    realtime_probe_ready: bool = False
    effective_send_ready: bool = False
    identifier_source_confidence: str = "low"  # high | medium | low
    readiness_reason_code: Optional[str] = None

    # Diagnostic timeline fields.
    probe_timestamp: Optional[str] = None
    consecutive_probe_failures: int = 0
    last_good_probe_at: Optional[str] = None
    last_good_imsi: Optional[str] = None


class ModemsDiscoverResponse(BaseModel):
    success: bool
    modems: List[ModemDiscoverItem]