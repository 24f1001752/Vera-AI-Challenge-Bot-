from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


Scope = Literal["category", "merchant", "customer", "trigger"]


class ContextPushRequest(BaseModel):
    scope: Scope
    context_id: str
    version: int = Field(ge=1)
    payload: Dict[str, Any]
    delivered_at: datetime


class ContextPushResponse(BaseModel):
    accepted: bool
    ack_id: Optional[str] = None
    stored_at: Optional[datetime] = None
    reason: Optional[str] = None
    current_version: Optional[int] = None
    details: Optional[str] = None


class TickRequest(BaseModel):
    now: datetime
    available_triggers: List[str] = Field(default_factory=list)


class TickAction(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    send_as: str
    trigger_id: str
    template_name: str
    template_params: List[str]
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: List[TickAction] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: Literal["merchant", "customer"]
    message: str
    received_at: datetime
    turn_number: int = Field(ge=1)


class ReplyResponseSend(BaseModel):
    action: Literal["send"]
    body: str
    cta: str
    rationale: str


class ReplyResponseWait(BaseModel):
    action: Literal["wait"]
    wait_seconds: int = Field(ge=1)
    rationale: str


class ReplyResponseClose(BaseModel):
    action: Literal["close"]
    rationale: str


ReplyResponse = ReplyResponseSend | ReplyResponseWait | ReplyResponseClose


class HealthzResponse(BaseModel):
    status: Literal["ok"]
    uptime_seconds: int
    contexts_loaded: Dict[Scope, int]


class MetadataResponse(BaseModel):
    team_name: str
    team_members: List[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str

