"""LLM diagnoses. Policy authorizes. This separation is the whole safety story.

The rulebook itself is per-company (config.policy). This module is the generic engine
that applies it: floor -> let-go -> preferred-with-eligibility. It never invents an
action; it can only select one the customer has already authorized.
"""

import operator
import re
from typing import Optional

from .taxonomy import (
    DEFAULT_CONFIDENCE_FLOOR,
    Intervention,
    Outcome,
    Policy,
    ProductConfig,
    UserContext,
)

# Back-compat default. The floor that actually governs a decision lives on config.policy;
# this is just the value the default Policy is built with.
CONFIDENCE_FLOOR = DEFAULT_CONFIDENCE_FLOOR


def decide(outcome: Outcome, config: ProductConfig, user: UserContext) -> Outcome:
    policy = config.policy
    floor = policy.confidence_floor

    if outcome.confidence < floor or outcome.reason == "unknown":
        outcome.intervention_id, outcome.savable = None, False
        outcome.rationale = (f"confidence {outcome.confidence:.2f} below floor "
                             f"{floor} -- deferring to your generic cancel flow")
        return outcome

    if outcome.reason in policy.let_go_reasons:
        outcome.savable = False
        outcome.intervention_id = _find(config, "pause")
        outcome.rationale = ("need genuinely ended -- offering a pause, not a discount. "
                             "Spending margin here retains no one.")
        return outcome

    for wanted in policy.preferred.get(outcome.reason, []):
        iv = _find_obj(config, wanted)
        if iv and _eligible(iv, outcome, user, policy):
            outcome.intervention_id = iv.id
            outcome.rationale = f"{outcome.reason} -> {iv.type}: {iv.description}"
            return outcome

    outcome.intervention_id, outcome.savable = None, False
    outcome.rationale = f"no authorized intervention matches {outcome.reason}"
    return outcome


def _eligible(iv: Intervention, outcome: Outcome, user: UserContext, policy: Policy) -> bool:
    # A discount is only ever legitimate for reasons the company authorized it for.
    if iv.type == "discount" and outcome.reason not in policy.discount_reasons:
        return False
    if not iv.eligible_when:
        return True
    scope = {"reason": outcome.reason, "confidence": outcome.confidence,
             "tenure": user.tenure_days, "mrr": user.mrr,
             "activated": user.activated, "logins": user.logins_last_30d,
             **user.signals}
    return _eval_rule(iv.eligible_when, scope)


# --------------------------------------------------------------------------------------
# Safe eligibility mini-language: OR of ANDs of `field OP value` atoms.
# Replaces the old eval()-based rule -- no code execution, no hardcoded taxonomy names.
# Example: "reason == price_value_mismatch AND tenure > 90"
# `value` is coerced to bool / number when it looks like one, else compared as a string.
# --------------------------------------------------------------------------------------
_OPS = [(">=", operator.ge), ("<=", operator.le), ("!=", operator.ne),
        ("==", operator.eq), ("=", operator.eq), (">", operator.gt), ("<", operator.lt)]


def _coerce(raw: str):
    raw = raw.strip().strip('"').strip("'")
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return float(raw)
    except ValueError:
        return raw


def _atom_ok(atom: str, scope: dict) -> bool:
    atom = atom.strip()
    if not atom:
        return True
    for sym, fn in _OPS:  # compound ops (>=, <=, !=, ==) are matched before their prefixes
        idx = atom.find(sym)
        if idx > 0:
            field_ = atom[:idx].strip()
            if field_ not in scope:
                return False
            left = scope[field_]
            right = _coerce(atom[idx + len(sym):])
            if isinstance(right, bool):
                if not isinstance(left, bool):
                    return False
            elif isinstance(right, (int, float)):
                if isinstance(left, bool) or not isinstance(left, (int, float)):
                    return False
            else:  # string comparison
                left = str(left)
            try:
                return bool(fn(left, right))
            except TypeError:
                return False
    return False


def _eval_rule(expr: str, scope: dict) -> bool:
    for clause in re.split(r"\bOR\b", expr):
        atoms = [a for a in re.split(r"\bAND\b", clause) if a.strip()]
        if atoms and all(_atom_ok(a, scope) for a in atoms):
            return True
    return False


def _find(config: ProductConfig, type_: str) -> Optional[str]:
    iv = _find_obj(config, type_)
    return iv.id if iv else None


def _find_obj(config: ProductConfig, type_: str) -> Optional[Intervention]:
    return next((i for i in config.interventions if i.type == type_), None)
