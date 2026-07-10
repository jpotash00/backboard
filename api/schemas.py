"""Request/response models for the session API (§4). These are the wire contract;
they mirror `engine.taxonomy.Outcome` and the SDK's `src/types.ts`."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class OfferSpec(BaseModel):
    """One authorized save offer. `type` is the intervention kind (discount/pause/downgrade/
    onboarding/roadmap/support/...); `id` is optional (auto-generated if omitted)."""
    type: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=400)
    id: Optional[str] = Field(default=None, max_length=80)
    eligible_when: Optional[str] = Field(default=None, max_length=400)
    # Optional structured handle applied verbatim on accept -- usually a reference to the offer's
    # already-created object in your billing system (Offboard only picks offers you authorized, so
    # it typically exists), e.g. {"stripe_coupon": "off50_3mo"}. Offboard never executes it; it's
    # echoed back to your onAccept handler so your billing code doesn't parse `description`.
    params: dict = Field(default_factory=dict)


class ProductFacts(BaseModel):
    """The four facts rendered into the interviewer's context — the only prose a customer
    must supply; everything else (taxonomy, policy, economics) comes from defaults."""
    product_name: str = Field(min_length=1, max_length=200)
    product_context: str = Field(min_length=1, max_length=2000)
    activation_definition: str = Field(min_length=1, max_length=500)
    pricing_summary: str = Field(min_length=1, max_length=1000)


class ConfigSpecRequest(BaseModel):
    """POST /configs body: the tiny spec that provisions a customer. Mirrors the
    `onboarding.provision` spec — product facts + the authorized offer menu; the server mints
    the signing_secret, fills taxonomy/policy from defaults, validates, persists, and
    hot-registers. The offer menu is declared, never inferred."""
    customer_id: str = Field(min_length=1, max_length=200)
    # Optional: omit (or send blank) and the server mints a random `pk_live_...` key. A public
    # key is public by design (it ships in the browser), so a random one just guarantees
    # uniqueness and keeps the customer roster non-enumerable. Provide one only to pin a
    # specific key (migration / testing).
    public_key: Optional[str] = Field(default=None, max_length=200)
    product: ProductFacts
    offers: list[OfferSpec] = Field(min_length=1)
    allowed_origins: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)


class ConfigUpdateRequest(BaseModel):
    """PATCH /configs/{id} body: the mutable part of a customer's config. Replaces product facts,
    the offer menu, competitors and origins. The IDENTITY fields are deliberately NOT here --
    customer_id is immutable, and public_key + signing_secret are preserved so a live SDK and any
    in-flight identity tokens keep working across an edit."""
    product: ProductFacts
    offers: list[OfferSpec] = Field(min_length=1)
    allowed_origins: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)


class CreateSessionRequest(BaseModel):
    """POST /sessions body. Only `user_id` is required; the richer the behavioral
    context, the better the diagnosis disambiguates the cover story (constraint #6).

    Note the trust boundary: for a customer with a signing secret, the economic fields
    below (plan/mrr/tenure_days/logins_last_30d/activated/signals) are IGNORED in favour of
    the signed `identity_token` -- the browser cannot be trusted to price its own save."""
    user_id: str = Field(min_length=1, max_length=200)
    plan: str = Field(default="unknown", max_length=100)
    mrr: float = 0.0
    tenure_days: int = 0
    logins_last_30d: int = 0
    activated: bool = False
    usage_summary: str = Field(default="", max_length=2000)
    # Product-specific behavioral tells that don't fit the fixed fields
    # (e.g. {"seats_used": 7}); rendered into the interviewer's context.
    signals: dict[str, Any] = Field(default_factory=dict)
    # A token minted by the customer's backend (api.identity.sign_identity) carrying the
    # trusted user economics. Required when the customer is configured with a signing secret;
    # ignored otherwise. This is what stops a browser from spoofing mrr to unlock a discount.
    identity_token: Optional[str] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    message: str


class TurnRequest(BaseModel):
    # Capped: an unbounded reply is a token-blowup / abuse vector, and no honest cancel-flow
    # answer runs to thousands of characters.
    user_message: str = Field(min_length=1, max_length=4000)


class ResolutionRequest(BaseModel):
    """What the user did with the offer. `accepted` is the realized save signal that,
    aggregated, recalibrates the policy's economic priors."""
    accepted: bool


class OutcomeReport(BaseModel):
    """POST /outcomes body: the customer's billing backend reporting whether a user is still
    subscribed at (or after) the experiment horizon. This is the downstream ground truth the
    holdout needs -- accept rate alone can't see post-accept churn or always-stayers.

    `user_id` must match the id used at session start so the readout can join it back to the
    arm and offer. `observed_at` is when the status was true (ISO 8601); the customer supplies
    it so a batch backfill reports real observation times, not ingestion time."""
    user_id: str = Field(min_length=1, max_length=200)
    active: bool
    observed_at: str = Field(min_length=1, max_length=64)


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
    # NOTE: Outcome.observations is deliberately NOT mirrored here. It's internal extraction
    # ("they don't know you can see this") that stays in the server-side logs; Pydantic drops
    # the extra key from asdict() so it never reaches the browser. Do not add it.


class InterventionModel(BaseModel):
    """The resolved offer, so the host can render it without re-fetching config.
    `outcome.intervention_id` is the id; this is that intervention, spelled out."""
    id: str
    type: str
    description: str
    # Structured params to apply in your billing system (empty when the offer defines none).
    params: dict = Field(default_factory=dict)


class TurnResponse(BaseModel):
    message: Optional[str] = None
    done: bool
    outcome: Optional[OutcomeModel] = None
    intervention: Optional[InterventionModel] = None
