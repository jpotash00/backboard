"""Request/response models for the session API (§4). These are the wire contract;
they mirror `engine.taxonomy.Outcome` and the SDK's `src/types.ts`."""

from typing import Any, Optional

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
    # Product-specific behavioral tells that don't fit the fixed fields
    # (e.g. {"seats_used": 7}); rendered into the interviewer's context.
    signals: dict[str, Any] = Field(default_factory=dict)


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
    # Declared decision audit (from deterministic policy, never the model): the money at
    # stake + margin spent, and every option considered with its cost/EV and verdict.
    economics: Optional[dict[str, Any]] = None
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)
    # "defer" | "suggest" | "act" -- how far policy will go on this confidence.
    mode: str = "defer"
    # Whether the behavioral data backed the diagnosis, and the confidence it adjusted to.
    corroboration: Optional[dict[str, Any]] = None


class InterventionModel(BaseModel):
    """The resolved offer, so the host can render it without re-fetching config.
    `outcome.intervention_id` is the id; this is that intervention, spelled out."""
    id: str
    type: str
    description: str


class TurnResponse(BaseModel):
    message: Optional[str] = None
    done: bool
    outcome: Optional[OutcomeModel] = None
    intervention: Optional[InterventionModel] = None
