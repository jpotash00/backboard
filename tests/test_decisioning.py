"""The economic decision layer: policy DECLARES the economics of every authorization
(economics + decision_trace) and, in expected_value mode, spends margin efficiently.

The model never reaches this code -- these all drive `decide()` with a fixed diagnosis."""

from engine import Intervention, Outcome, Policy, ProductConfig, Scoring, decide

FULL_MENU = [
    Intervention("discount_50_3mo", "discount", "50% off 3mo",
                 "reason == price_value_mismatch AND tenure > 90"),
    Intervention("downgrade_starter", "downgrade", "downgrade"),
    Intervention("pause_3mo", "pause", "pause 3 months"),
    Intervention("setup_call_15m", "onboarding", "15-min setup call"),
    Intervention("roadmap_notify", "roadmap", "notify when it ships"),
    Intervention("priority_support", "support", "priority support"),
]


def cfg(rank_by="preferred", scoring=None, interventions=None):
    return ProductConfig(
        product_name="Acme", product_context="", activation_definition="", pricing_summary="",
        interventions=FULL_MENU if interventions is None else interventions,
        policy=Policy(rank_by=rank_by, scoring=scoring or Scoring()),
    )


def outcome(reason, confidence=0.9):
    return Outcome(reason=reason, confidence=confidence, evidence="", cover_story="x",
                   savable=True, intervention_id=None, rationale="", turns_used=1)


def user(**kw):
    base = dict(user_id="u", plan="Growth", mrr=49.0, tenure_days=200,
                logins_last_30d=20, activated=True, usage_summary="")
    base.update(kw)
    from engine import UserContext
    return UserContext(**base)


def entry_for(o, iv_id):
    return next(e for e in o.decision_trace if e.get("intervention_id") == iv_id)


# --- DECLARE: every decision carries an auditable trace + economics ---

def test_decision_declares_economics():
    o = decide(outcome("price_value_mismatch"), cfg(), user(mrr=49))
    assert o.economics["customer_value"] == 49.0 * 12
    assert o.economics["rank_by"] == "preferred"
    # margin spent is the chosen action's cost (discount = 75 by default)
    assert o.economics["margin_spent"] == 75.0
    assert o.economics["chosen_expected_value"] == o.economics["chosen_expected_value"]  # present


def test_trace_marks_the_chosen_and_explains_rejections():
    o = decide(outcome("never_activated"), cfg(), user(tenure_days=365))
    chosen = [e for e in o.decision_trace if e.get("chosen")]
    assert len(chosen) == 1 and chosen[0]["type"] == "onboarding"
    # discount isn't even in never_activated's menu order -> it should not appear as chosen;
    # every considered option records eligibility + a rejection reason when excluded.
    for e in o.decision_trace:
        assert "eligible" in e
        if not e["eligible"]:
            assert e.get("rejected")


def test_discount_gate_is_declared_in_trace_for_switched_competitor():
    # switched_competitor preferred = [discount, roadmap]; discount is gated off by
    # discount_reasons -> the trace must SAY why, not silently skip it.
    o = decide(outcome("switched_competitor"), cfg(), user())
    disc = entry_for(o, "discount_50_3mo")
    assert disc["eligible"] is False and "ineligible" in disc["rejected"]
    assert o.intervention_id == "roadmap_notify"


def test_defer_below_floor_is_traced():
    o = decide(outcome("price_value_mismatch", confidence=0.4), cfg(), user())
    assert o.intervention_id is None
    assert o.decision_trace[0]["gate"] == "confidence_floor"


# --- RANK: expected_value mode spends margin efficiently ---

def test_ev_mode_matches_preferred_for_a_healthy_value_user():
    # A $49/mo price churner: the discount still has the best EV, so both modes agree.
    pref = decide(outcome("price_value_mismatch"), cfg("preferred"), user(mrr=49))
    ev = decide(outcome("price_value_mismatch"), cfg("expected_value"), user(mrr=49))
    assert pref.intervention_id == "discount_50_3mo"
    assert ev.intervention_id == "discount_50_3mo"


def test_ev_mode_declines_the_discount_for_a_low_value_user():
    # A $8/mo user isn't worth a margin-burning discount. preferred mode fires it anyway;
    # expected_value mode picks a cheaper action -- offer efficiency, declared.
    low = user(mrr=8, tenure_days=200)
    pref = decide(outcome("price_value_mismatch"), cfg("preferred"), low)
    ev = decide(outcome("price_value_mismatch"), cfg("expected_value"), low)
    assert pref.intervention_id == "discount_50_3mo"          # curated order fires discount
    assert ev.intervention_id != "discount_50_3mo"            # EV declines to burn margin
    assert entry_for(ev, "discount_50_3mo")["expected_value"] < 0
    assert ev.economics["margin_spent"] < pref.economics["margin_spent"]


# --- cost-tiered confidence: a costly action can demand more certainty ---

def test_expensive_action_can_require_higher_confidence():
    scoring = Scoring(min_confidence_by_type={"discount": 0.9})
    # confidence 0.7 clears the global floor but not the discount's own 0.9 bar.
    o = decide(outcome("price_value_mismatch", confidence=0.7), cfg(scoring=scoring),
               user(tenure_days=200))
    disc = entry_for(o, "discount_50_3mo")
    assert disc["eligible"] is False and "required" in disc["rejected"]
    assert o.intervention_id == "downgrade_starter"          # falls through to the next option
