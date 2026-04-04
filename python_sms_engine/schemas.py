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
    reachable: bool
    at_ok: bool


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


class ModemsDiscoverResponse(BaseModel):
    success: bool
    modems: List[ModemDiscoverItem]