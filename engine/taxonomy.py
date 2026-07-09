"""The deterministic spine: a fixed churn taxonomy the LLM must resolve into.
The conversation is free-form. The destination is not."""

from dataclasses import dataclass, field
from typing import Literal, Optional

Reason = Literal[
    "never_activated", "value_ended", "price_value_mismatch", "missing_capability",
    "switched_competitor", "product_quality", "involuntary", "unknown",
]

COVER_STORIES = [
    "too_expensive", "not_using_it", "found_alternative",
    "missing_feature", "too_complicated", "no_reason_given",
]


@dataclass
class Intervention:
    """An action the CUSTOMER has authorized. We can never invent one."""
    id: str
    type: Literal["discount", "pause", "downgrade", "onboarding", "roadmap", "support", "none"]
    description: str
    eligible_when: Optional[str] = None


@dataclass
class ProductConfig:
    """Injected per-customer. This -- not the questions -- is what differs between
    a design tool and a meditation app."""
    product_name: str
    product_context: str
    activation_definition: str
    pricing_summary: str
    known_churn_reasons: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    interventions: list[Intervention] = field(default_factory=list)


@dataclass
class UserContext:
    """Behavioral signal. This is what disambiguates the cover story."""
    user_id: str
    plan: str
    mrr: float
    tenure_days: int
    logins_last_30d: int
    activated: bool
    usage_summary: str = ""


@dataclass
class Outcome:
    """`cover_story` vs `reason` is the entire pitch in one field."""
    reason: Reason
    confidence: float
    evidence: str
    cover_story: str
    savable: bool
    intervention_id: Optional[str]
    rationale: str
    turns_used: int
