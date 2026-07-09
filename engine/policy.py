"""LLM diagnoses. Policy authorizes. This separation is the whole safety story."""

from typing import Optional
from .taxonomy import Intervention, Outcome, ProductConfig, UserContext

CONFIDENCE_FLOOR = 0.6

# Note what's absent: never_activated. Discounting someone who never got value
# is strictly worse than helping them get it.
DISCOUNT_FIXES = {"price_value_mismatch"}

# The honest move is to let them go cleanly, warm door open.
LET_GO = {"value_ended"}

PREFERRED: dict[str, list[str]] = {
    "never_activated":      ["onboarding", "support", "pause"],
    "price_value_mismatch": ["discount", "downgrade", "pause"],
    "value_ended":          ["pause", "none"],
    "missing_capability":   ["roadmap", "none"],
    "switched_competitor":  ["discount", "roadmap"],
    "product_quality":      ["support", "discount"],
    "involuntary":          ["support"],
    "unknown":              [],
}


def _eligible(iv: Intervention, outcome: Outcome, user: UserContext) -> bool:
    if iv.type == "discount" and outcome.reason not in DISCOUNT_FIXES:
        return False
    if not iv.eligible_when:
        return True
    scope = {"reason": outcome.reason, "confidence": outcome.confidence,
             "tenure": user.tenure_days, "mrr": user.mrr,
             "activated": user.activated, "logins": user.logins_last_30d}
    expr = (iv.eligible_when.replace("=", "==").replace(">==", ">=").replace("<==", "<=")
            .replace("AND", "and").replace("OR", "or"))
    for w in ("never_activated", "value_ended", "price_value_mismatch", "missing_capability",
              "switched_competitor", "product_quality", "involuntary", "unknown", "price"):
        expr = expr.replace(f"=={w}", f'=="{w}"')
    expr = expr.replace('=="price"', '=="price_value_mismatch"')
    try:
        return bool(eval(expr, {"__builtins__": {}}, scope))
    except Exception:
        return False


def decide(outcome: Outcome, config: ProductConfig, user: UserContext) -> Outcome:
    if outcome.confidence < CONFIDENCE_FLOOR or outcome.reason == "unknown":
        outcome.intervention_id, outcome.savable = None, False
        outcome.rationale = (f"confidence {outcome.confidence:.2f} below floor "
                             f"{CONFIDENCE_FLOOR} -- deferring to your generic cancel flow")
        return outcome

    if outcome.reason in LET_GO:
        outcome.savable = False
        outcome.intervention_id = _find(config, "pause")
        outcome.rationale = ("need genuinely ended -- offering a pause, not a discount. "
                             "Spending margin here retains no one.")
        return outcome

    for wanted in PREFERRED.get(outcome.reason, []):
        iv = _find_obj(config, wanted)
        if iv and _eligible(iv, outcome, user):
            outcome.intervention_id = iv.id
            outcome.rationale = f"{outcome.reason} -> {iv.type}: {iv.description}"
            return outcome

    outcome.intervention_id, outcome.savable = None, False
    outcome.rationale = f"no authorized intervention matches {outcome.reason}"
    return outcome


def _find(config: ProductConfig, type_: str) -> Optional[str]:
    iv = _find_obj(config, type_)
    return iv.id if iv else None


def _find_obj(config: ProductConfig, type_: str) -> Optional[Intervention]:
    return next((i for i in config.interventions if i.type == type_), None)
