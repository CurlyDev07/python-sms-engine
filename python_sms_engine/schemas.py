from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator


class SendRequest(BaseModel):
    sim_id: StrictInt
    phone: StrictStr
    message: StrictStr
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("phone must not be empty")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be empty")
        return value


class SmsSendResult(BaseModel):
    success: bool
    message_id: Optional[int] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    success: bool
    service: str
    status: str


class ModemHealthItem(BaseModel):
    sim_id: int
    port: str
    reachable: bool
    at_ok: bool


class ModemsHealthResponse(BaseModel):
    success: bool
    modems: List[ModemHealthItem]
