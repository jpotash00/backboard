"""The policy layer is the safety story: LLM diagnoses, policy authorizes. These lock
its load-bearing invariants so they can never silently regress as the system grows."""

import pytest

from engine import Intervention, Outcome, ProductConfig, UserContext, decide
from engine.policy import CONFIDENCE_FLOOR


def make_outcome(reason, confidence=0.9, cover_story="too_expensive"):
    return Outcome(
        reason=reason, confidence=confidence, evidence="", cover_story=cover_story,
        savable=True, intervention_id=None, rationale="", turns_used=1,
    )


def make_user(**kw):
    base = dict(user_id="u", plan="Growth", mrr=199.0, tenure_days=200,
                logins_last_30d=20, activated=True, usage_summary="")
    base.update(kw)
    return UserContext(**base)


FULL_MENU = [
    Intervention("discount_50_3mo", "discount", "50% off 3mo", "reason=price AND tenure>90"),
    Intervention("downgrade_starter", "downgrade", "downgrade"),
    Intervention("pause_3mo", "pause", "pause 3 months"),
    Intervention("setup_call_15m", "onboarding", "15-min setup call"),
    Intervention("roadmap_notify", "roadmap", "notify when it ships"),
    Intervention("priority_support", "support", "priority support"),
]


def config(interventions=None):
    return ProductConfig(
        product_name="Acme", product_context="", activation_definition="",
        pricing_summary="", interventions=interventions if interventions is not None else FULL_MENU,
    )


# --- The core safety invariant: never discount someone who never got value. ---

def test_never_activated_never_gets_a_discount():
    """The single most important rule in the whole system."""
    out = decide(make_outcome("never_activated"), config(), make_user(tenure_days=365))
    iv = next(i for i in FULL_MENU if i.id == out.intervention_id)
    assert iv.type != "discount"
    assert iv.type == "onboarding"


def test_never_activated_never_discounts_even_if_menu_is_discount_only():
    out = decide(
        make_outcome("never_activated"),
        config([FULL_MENU[0]]),  # only a discount available
        make_user(tenure_days=365),
    )
    assert out.intervention_id is None  # better to offer nothing than to discount


# --- price_value_mismatch is the ONLY path to a discount. ---

def test_price_value_mismatch_reaches_discount_when_eligible():
    out = decide(make_outcome("price_value_mismatch"), config(), make_user(tenure_days=200))
    assert out.intervention_id == "discount_50_3mo"


def test_discount_gated_by_eligible_when_tenure():
    """eligible_when: reason=price AND tenure>90 -- a 10-day account must not qualify."""
    out = decide(make_outcome("price_value_mismatch"), config(), make_user(tenure_days=10))
    # Falls through discount (ineligible) to the next preferred type.
    assert out.intervention_id != "discount_50_3mo"
    assert out.intervention_id == "downgrade_starter"


# --- value_ended: let them go cleanly, pause not discount, unsavable. ---

def test_value_ended_offers_pause_and_marks_unsavable():
    out = decide(make_outcome("value_ended"), config(), make_user())
    assert out.intervention_id == "pause_3mo"
    assert out.savable is False


# --- The confidence floor defers to the caller's generic flow. ---

def test_below_confidence_floor_returns_no_intervention():
    out = decide(make_outcome("price_value_mismatch", confidence=0.4), config(), make_user())
    assert out.intervention_id is None
    assert out.savable is False
    assert "below floor" in out.rationale


def test_at_confidence_floor_is_allowed():
    out = decide(
        make_outcome("price_value_mismatch", confidence=CONFIDENCE_FLOOR),
        config(), make_user(tenure_days=200),
    )
    assert out.intervention_id == "discount_50_3mo"


def test_unknown_reason_defers():
    out = decide(make_outcome("unknown", confidence=0.99), config(), make_user())
    assert out.intervention_id is None


# --- Menu gaps degrade to "nothing", never to a wrong action. ---

def test_missing_capability_with_empty_menu_offers_nothing():
    out = decide(make_outcome("missing_capability"), config([]), make_user())
    assert out.intervention_id is None


@pytest.mark.parametrize("reason,expected_type", [
    ("never_activated", "onboarding"),
    ("price_value_mismatch", "discount"),
    ("value_ended", "pause"),
    ("missing_capability", "roadmap"),
    ("switched_competitor", "roadmap"),   # discount ineligible (reason != price) -> roadmap
    ("product_quality", "support"),
    ("involuntary", "support"),
])
def test_full_menu_maps_each_reason_to_expected_action(reason, expected_type):
    out = decide(make_outcome(reason), config(), make_user(tenure_days=200))
    iv = next((i for i in FULL_MENU if i.id == out.intervention_id), None)
    assert iv is not None and iv.type == expected_type
