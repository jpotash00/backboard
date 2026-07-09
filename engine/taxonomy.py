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


def _default_type_cost() -> dict[str, float]:
    """Suggested $ cost to the business of *making* each kind of offer. Rough, opinionated
    defaults -- tune them to your margins. A discount gives up real money; a roadmap
    notification costs nothing; onboarding/support cost a person's time."""
    return {
        "discount": 75.0, "downgrade": 30.0, "pause": 10.0,
        "onboarding": 15.0, "support": 15.0, "roadmap": 0.0,
        "gift": 20.0, "extend_trial": 5.0, "none": 0.0,
    }


def _default_save_prior() -> dict[str, float]:
    """Suggested P(save) that this churner is saveable AT ALL when acted on -- keyed by
    reason. The starting point; the compounding data asset (runs/*.jsonl) is what you
    recalibrate these from."""
    return {
        "never_activated": 0.35, "price_value_mismatch": 0.50, "value_ended": 0.10,
        "missing_capability": 0.25, "switched_competitor": 0.20,
        "product_quality": 0.40, "involuntary": 0.90, "unknown": 0.0,
    }


def _default_effectiveness() -> dict[str, float]:
    """How well each action TYPE converts a saveable churner, 0..1. Without this, EV would
    just minimize cost (save odds would be constant across options). A discount converts a
    price churner well; a pause holds fewer; a roadmap-notify barely any."""
    return {
        "discount": 0.9, "downgrade": 0.7, "onboarding": 0.8, "support": 0.7,
        "pause": 0.5, "gift": 0.6, "extend_trial": 0.5, "roadmap": 0.3, "none": 0.0,
    }


@dataclass
class Scoring:
    """The SUGGESTED economics behind each decision. Every value is an opinionated default;
    you fine-tune the numbers, never the logic. Two uses:

      1. DECLARE -- the economics of every decision are written into `Outcome.decision_trace`
         (what each option would cost, its expected value), so spend is auditable.
      2. RANK -- when `Policy.rank_by == "expected_value"`, options are chosen to maximize
         EV = P(save | reason) * customer_value - cost(action), where
         customer_value = user.mrr * value_horizon_months (an LTV proxy).

    This is the offer-efficiency knob: same saves, less margin spent."""
    value_horizon_months: int = 12
    type_cost: dict[str, float] = field(default_factory=_default_type_cost)
    save_prior: dict[str, float] = field(default_factory=_default_save_prior)
    type_effectiveness: dict[str, float] = field(default_factory=_default_effectiveness)
    default_type_cost: float = 10.0
    default_save_prior: float = 0.20
    default_effectiveness: float = 0.5
    # Optional per-type confidence overrides: a costly action can demand more certainty than
    # the global floor (a wrong discount burns money; a wrong roadmap-notify costs nothing).
    # Empty by default -- the global floor governs everything.
    min_confidence_by_type: dict[str, float] = field(default_factory=dict)

    def cost_of(self, type_: str) -> float:
        return self.type_cost.get(type_, self.default_type_cost)

    def save_probability(self, reason: str) -> float:
        return self.save_prior.get(reason, self.default_save_prior)

    def effectiveness_of(self, type_: str) -> float:
        return self.type_effectiveness.get(type_, self.default_effectiveness)

    def expected_value(self, reason: str, type_: str, customer_value: float) -> float:
        """EV = P(saveable) * P(this action converts) * value_at_stake - cost."""
        p = min(1.0, self.save_probability(reason) * self.effectiveness_of(type_))
        return round(p * customer_value - self.cost_of(type_), 2)

    def required_confidence(self, type_: str, floor: float) -> float:
        return max(floor, self.min_confidence_by_type.get(type_, 0.0))


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
    # How to pick among eligible options. "preferred" = the curated per-reason order above
    # (the safe, suggested default). "expected_value" = maximize EV via `scoring` below.
    rank_by: str = "preferred"
    scoring: Scoring = field(default_factory=Scoring)


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
        if self.policy.rank_by not in ("preferred", "expected_value"):
            raise ValueError("policy.rank_by must be 'preferred' or 'expected_value'")
        sc = self.policy.scoring
        if sc.value_horizon_months <= 0:
            raise ValueError("scoring.value_horizon_months must be positive")
        for t, c in sc.min_confidence_by_type.items():
            if not 0.0 <= c <= 1.0:
                raise ValueError(f"scoring.min_confidence_by_type['{t}'] must be in [0, 1]")
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
    # Declared by policy (not the model): a full audit trail of the authorization.
    # `economics` summarizes the money at stake + margin spent; `decision_trace` lists
    # every option considered, its cost/EV, and why it won or was rejected.
    economics: Optional[dict] = None
    decision_trace: list = field(default_factory=list)
