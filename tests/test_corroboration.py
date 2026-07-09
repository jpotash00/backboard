"""Corroboration (does the behavioral data back the diagnosis?) and the action tiers
(defer / suggest / act). Both are declared, deterministic, and never touch the model."""

from engine import Intervention, Outcome, Policy, ProductConfig, Scoring, UserContext, decide

FULL_MENU = [
    Intervention("discount_50_3mo", "discount", "50% off 3mo",
                 "reason == price_value_mismatch AND tenure > 90"),
    Intervention("downgrade_starter", "downgrade", "downgrade"),
    Intervention("pause_3mo", "pause", "pause 3 months"),
    Intervention("setup_call_15m", "onboarding", "15-min setup call"),
    Intervention("roadmap_notify", "roadmap", "notify when it ships"),
    Intervention("priority_support", "support", "priority support"),
]


def cfg(policy=None):
    return ProductConfig(
        product_name="Acme", product_context="", activation_definition="", pricing_summary="",
        interventions=FULL_MENU, policy=policy or Policy(),
    )


def outcome(reason, confidence=0.9):
    return Outcome(reason=reason, confidence=confidence, evidence="", cover_story="x",
                   savable=True, intervention_id=None, rationale="", turns_used=1)


def user(**kw):
    base = dict(user_id="u", plan="Growth", mrr=49.0, tenure_days=200,
                logins_last_30d=20, activated=True, usage_summary="")
    base.update(kw)
    return UserContext(**base)


# --- corroboration status ---

def test_contradicted_penalizes_effective_confidence():
    # never_activated expects activated == false; this user IS activated -> contradiction.
    o = decide(outcome("never_activated", 0.9), cfg(), user(activated=True))
    assert o.corroboration["status"] == "contradicted"
    assert o.corroboration["raw_confidence"] == 0.9
    assert o.corroboration["effective_confidence"] == 0.65   # 0.9 - 0.25 penalty
    # still above floor, so it still acts (a suggestion), just with less certainty
    assert o.intervention_id == "setup_call_15m"


def test_corroborated_leaves_confidence_untouched():
    o = decide(outcome("never_activated", 0.9), cfg(), user(activated=False, logins_last_30d=1))
    assert o.corroboration["status"] == "corroborated"
    assert o.corroboration["penalty"] == 0.0
    assert o.corroboration["effective_confidence"] == 0.9


def test_unverified_when_no_rule_for_reason():
    # product_quality has no default corroboration rule.
    o = decide(outcome("product_quality", 0.9), cfg(), user())
    assert o.corroboration["status"] == "unverified"
    assert o.corroboration["penalty"] == 0.0


def test_custom_reason_without_rule_is_unverified():
    o = decide(outcome("some_custom_reason", 0.9), cfg(), user())
    assert o.corroboration["status"] == "unverified"


def test_contradiction_can_push_below_the_floor_and_defer():
    # 0.7 raw - 0.25 penalty = 0.45 < 0.6 floor -> the confident-and-wrong guard fires.
    o = decide(outcome("never_activated", 0.7), cfg(), user(activated=True))
    assert o.corroboration["status"] == "contradicted"
    assert o.mode == "defer"
    assert o.intervention_id is None
    assert "contradicted by behavioral data" in o.rationale


def test_penalty_is_configurable_off():
    pol = Policy(contradiction_penalty=0.0)
    o = decide(outcome("never_activated", 0.9), cfg(pol), user(activated=True))
    assert o.corroboration["status"] == "contradicted"
    assert o.corroboration["effective_confidence"] == 0.9      # flagged, but not penalized


# --- action tiers: defer / suggest / act ---

def test_mode_tiers_by_effective_confidence():
    # price_value_mismatch corroborates when activated == true.
    act = decide(outcome("price_value_mismatch", 0.9), cfg(), user(activated=True))
    suggest = decide(outcome("price_value_mismatch", 0.7), cfg(), user(activated=True))
    defer = decide(outcome("price_value_mismatch", 0.5), cfg(), user(activated=True))
    assert act.mode == "act"           # >= 0.85 act bar
    assert suggest.mode == "suggest"   # between floor (0.6) and act (0.85)
    assert defer.mode == "defer"       # below floor
    assert act.intervention_id == "discount_50_3mo"
    assert suggest.intervention_id == "discount_50_3mo"   # still chosen, just recommended
    assert defer.intervention_id is None


def test_act_bar_is_tunable():
    strict = Policy(act_confidence=0.95)   # demand near-certainty to auto-apply
    o = decide(outcome("price_value_mismatch", 0.9), cfg(strict), user(activated=True))
    assert o.mode == "suggest"             # 0.9 < 0.95 -> recommend, don't auto-act


def test_contradiction_downgrades_act_to_suggest():
    # A contradiction that stays above the floor still lowers effective confidence, which
    # can drop a would-be "act" to "suggest" -- exactly the intended safety behavior.
    # never_activated: raw 0.95 corroborated would 'act'; contradicted -> 0.70 -> 'suggest'.
    o = decide(outcome("never_activated", 0.95), cfg(), user(activated=True))
    assert o.corroboration["status"] == "contradicted"
    assert o.mode == "suggest"
    assert o.intervention_id == "setup_call_15m"
