"""The deterministic spine: a per-customer churn taxonomy the LLM must resolve into.
The conversation is free-form. The destination is not.

Everything a specific company needs to tailor -- its churn reasons, its resolution
menu, and the policy that maps one to the other -- lives on ProductConfig. The engine
(interviewer + policy) is generic; swap the config and it becomes a different company's
cancel flow. Nothing in this module is SaaS-specific except the *defaults*.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# The default SaaS taxonomy ids. Kept as a Literal purely so the eval fixtures and
# scoring stay type-checked; a real customer supplies its own `reasons` list and the
# engine treats reason ids as opaque strings throughout.
Reason = Literal[
    "never_activated", "value_ended", "price_value_mismatch", "missing_capability",
    "switched_competitor", "product_quality", "involuntary", "unknown",
]

DEFAULT_CONFIDENCE_FLOOR = 0.6

COVER_STORIES = [
    "too_expensive", "not_using_it", "found_alternative",
    "missing_feature", "too_complicated", "no_reason_given",
]


@dataclass
class ReasonDef:
    """One resolvable churn reason. `description` is shown to the model verbatim, so it
    IS the prompt spec for this reason -- write it the way you'd brief a human."""
    id: str
    description: str


# The standard SaaS taxonomy, used when a ProductConfig doesn't supply its own `reasons`.
DEFAULT_REASONS: list[ReasonDef] = [
    ReasonDef("never_activated", "signed up, never reached first value"),
    ReasonDef("value_ended", "need genuinely finished (project done, left company, seasonal)"),
    ReasonDef("price_value_mismatch", "got real value, doesn't justify cost"),
    ReasonDef("missing_capability", "needed something we don't do"),
    ReasonDef("switched_competitor", "someone else won them"),
    ReasonDef("product_quality", "bugs, reliability, support failures"),
    ReasonDef("involuntary", "payment failure, not a real churn decision"),
    ReasonDef("unknown", "cannot resolve; be honest about this"),
]


@dataclass
class Intervention:
    """An action the CUSTOMER has authorized. We can never invent one.

    `type` is an open string (common: discount, pause, downgrade, onboarding, roadmap,
    support, gift, extend_trial, none) so a company can define its own resolution kinds;
    Policy.preferred just has to reference the same strings.
    """
    id: str
    type: str
    description: str
    eligible_when: Optional[str] = None


def _default_preferred() -> dict[str, list[str]]:
    """Reason id -> resolution types to try, best first. The default SaaS rulebook."""
    return {
        "never_activated":      ["onboarding", "support", "pause"],
        "price_value_mismatch": ["discount", "downgrade", "pause"],
        "value_ended":          ["pause", "none"],
        "missing_capability":   ["roadmap", "none"],
        "switched_competitor":  ["discount", "roadmap"],
        "product_quality":      ["support", "discount"],
        "involuntary":          ["support"],
        "unknown":              [],
    }


@dataclass
class Policy:
    """The business rulebook -- per company. Which resolution each reason earns, which
    reasons may be bought back with margin, and how sure the model must be to act at all.
    Two companies with the same taxonomy can still run opposite policies here."""
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
    preferred: dict[str, list[str]] = field(default_factory=_default_preferred)
    # Reasons where spending margin (a discount) is legitimate. Discounting anyone else
    # -- e.g. someone who never activated -- is strictly worse than helping them.
    discount_reasons: set[str] = field(default_factory=lambda: {"price_value_mismatch"})
    # Reasons where the honest move is to let them go cleanly, warm door open.
    let_go_reasons: set[str] = field(default_factory=lambda: {"value_ended"})


@dataclass
class ProductConfig:
    """Injected per-customer. This -- not the questions -- is what differs between a
    design tool and a meditation app. In production it's a stored, versioned record
    (eventually auto-proposed from the customer's pricing page + docs)."""
    product_name: str
    product_context: str
    activation_definition: str
    pricing_summary: str
    known_churn_reasons: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    interventions: list[Intervention] = field(default_factory=list)
    reasons: list[ReasonDef] = field(default_factory=lambda: list(DEFAULT_REASONS))
    policy: Policy = field(default_factory=Policy)
    config_version: str = "1"

    def reason_ids(self) -> list[str]:
        return [r.id for r in self.reasons]

    def validate(self) -> "ProductConfig":
        """Fail fast on an internally inconsistent config -- the guardrail that makes
        per-customer configs safe to accept from a form or a scraper. Returns self so
        it can wrap a construction: ProductConfig(...).validate()."""
        ids = self.reason_ids()
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate reason id in config.reasons")
        iv_ids = [i.id for i in self.interventions]
        if len(iv_ids) != len(set(iv_ids)):
            raise ValueError("duplicate intervention id in config.interventions")
        if not 0.0 <= self.policy.confidence_floor <= 1.0:
            raise ValueError("policy.confidence_floor must be in [0, 1]")
        known = set(ids)
        for rid in self.policy.preferred:
            if rid not in known:
                raise ValueError(f"policy.preferred references unknown reason '{rid}'")
        for r in self.reasons:
            if r.id not in self.policy.preferred:
                raise ValueError(f"reason '{r.id}' has no policy.preferred entry")
        for rid in self.policy.discount_reasons | self.policy.let_go_reasons:
            if rid not in known:
                raise ValueError(f"policy references unknown reason '{rid}'")
        return self


@dataclass
class UserContext:
    """Behavioral signal. This is what disambiguates the cover story. `signals` is the
    open escape hatch for product-specific tells (seats_used, workouts_logged, ...) that
    don't fit the generic fields; they're rendered into the interviewer's context too."""
    user_id: str
    plan: str
    mrr: float
    tenure_days: int
    logins_last_30d: int
    activated: bool
    usage_summary: str = ""
    signals: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    """`cover_story` vs `reason` is the entire pitch in one field. `reason` is a config
    reason id (opaque string), not a fixed enum -- the taxonomy is per-customer."""
    reason: str
    confidence: float
    evidence: str
    cover_story: str
    savable: bool
    intervention_id: Optional[str]
    rationale: str
    turns_used: int
