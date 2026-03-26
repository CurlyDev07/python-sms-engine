from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class SendRequest(BaseModel):
    sim_id: str   # 🔥 CHANGED
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
    sim_id: Optional[str] = None   # 🔥 CHANGED
    port: str
    reachable: bool
    at_ok: bool


class ModemsHealthResponse(BaseModel):
    success: bool
    modems: List[ModemHealthItem]