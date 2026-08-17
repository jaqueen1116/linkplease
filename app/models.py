from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

class RuleCreate(BaseModel):
    keyword: str = Field(..., description="Keyword to match in comments")
    dm_message: str = Field(..., description="Message to send via DM")

class RuleResponse(BaseModel):
    rule_id: str
    keyword: str
    dm_message: str

class StatsResponse(BaseModel):
    sent: int
    failed: int
    queued: int
    duplicates_blocked: int

class WebhookResponse(BaseModel):
    status: str = "ok"
    event_id: Optional[str] = None
