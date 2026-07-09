"""Request/response models for the session API (§4). These are the wire contract;
they mirror `engine.taxonomy.Outcome` and the SDK's `src/types.ts`."""

from typing import Optional

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    """POST /sessions body. Only `user_id` is required; the richer the behavioral
    context, the better the diagnosis disambiguates the cover story (constraint #6)."""
    user_id: str
    plan: str = "unknown"
    mrr: float = 0.0
    tenure_days: int = 0
    logins_last_30d: int = 0
    activated: bool = False
    usage_summary: str = ""


class CreateSessionResponse(BaseModel):
    session_id: str
    message: str


class TurnRequest(BaseModel):
    user_message: str = Field(min_length=1)


class OutcomeModel(BaseModel):
    """The product. `cover_story` vs `reason` is the whole pitch in one field."""
    reason: str
    confidence: float
    evidence: str
    cover_story: str
    savable: bool
    intervention_id: Optional[str]
    rationale: str
    turns_used: int


class TurnResponse(BaseModel):
    message: Optional[str] = None
    done: bool
    outcome: Optional[OutcomeModel] = None
