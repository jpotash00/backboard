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
    """Authorize an action (or none), and DECLARE why. Every path writes a full audit
    trail onto the outcome: `economics` (value at stake, margin spent) and
    `decision_trace` (every option considered, its cost/EV, and why it won or lost).
    The model never reaches this code -- it only supplied the diagnosis being acted on."""
    policy = config.policy
    scoring = policy.scoring
    floor = policy.confidence_floor
    value = round(user.mrr * scoring.value_horizon_months, 2)
    p_save = scoring.save_probability(outcome.reason)

    # Corroboration: does the behavioral data back the model's story? If it contradicts,
    # penalize confidence -- this is the guard on the confident-and-wrong case. We gate on
    # the resulting EFFECTIVE confidence, never touching the model's raw self-report.
    corr = _corroborate(outcome, policy, user)
    penalty = policy.contradiction_penalty if corr["status"] == "contradicted" else 0.0
    eff = max(0.0, round(outcome.confidence - penalty, 4))
    corr.update(raw_confidence=outcome.confidence, penalty=penalty,
                effective_confidence=eff)
    outcome.corroboration = corr

    outcome.economics = {
        "customer_value": value,
        "value_horizon_months": scoring.value_horizon_months,
        "save_probability": p_save,
        "rank_by": policy.rank_by,
        "effective_confidence": eff,
        "margin_spent": 0.0,
    }
    trace: list = []

    # 1. Floor -- on EFFECTIVE confidence, so a data-contradicted story can defer. An honest
    #    "I don't know" beats a confident wrong save.
    if eff < floor or outcome.reason == "unknown":
        outcome.intervention_id, outcome.savable = None, False
        outcome.mode = "defer"
        why = f"contradicted by behavioral data ({corr['rule']}); " if penalty else ""
        outcome.rationale = (f"{why}effective confidence {eff:.2f} below floor {floor} "
                             f"-- deferring to your generic cancel flow")
        trace.append({"decision": "defer", "gate": "confidence_floor",
                      "effective_confidence": eff, "floor": floor,
                      "corroboration": corr["status"]})
        outcome.decision_trace = trace
        return outcome

    # 2. Let-go reasons -- don't fight it; offer a pause, warm door open.
    if outcome.reason in policy.let_go_reasons:
        pause_id = _find(config, "pause")
        outcome.savable = False
        outcome.intervention_id = pause_id
        outcome.mode = _mode(eff, policy)
        outcome.rationale = ("need genuinely ended -- offering a pause, not a discount. "
                             "Spending margin here retains no one.")
        outcome.economics["margin_spent"] = scoring.cost_of("pause") if pause_id else 0.0
        trace.append({"decision": "let_go", "reason": outcome.reason,
                      "intervention_id": pause_id})
        outcome.decision_trace = trace
        return outcome

    # 3. Score every option in the reason's menu: eligibility, cost, expected value.
    candidates = []  # (Intervention | None, trace_entry)
    for rank, wanted in enumerate(policy.preferred.get(outcome.reason, [])):
        iv = _find_obj(config, wanted)
        entry: dict = {"type": wanted, "rank": rank,
                       "intervention_id": iv.id if iv else None}
        if iv is None:
            entry.update(eligible=False, rejected="no intervention of this type in the menu")
            candidates.append((None, entry))
            continue
        required = scoring.required_confidence(iv.type, floor)
        entry.update(cost=scoring.cost_of(iv.type),
                     effectiveness=scoring.effectiveness_of(iv.type),
                     expected_value=scoring.expected_value(outcome.reason, iv.type, value),
                     required_confidence=required)
        if not _eligible(iv, outcome, user, policy):
            rule = iv.eligible_when or "discount-reason gate"
            entry.update(eligible=False, rejected=f"ineligible ({rule})")
            candidates.append((None, entry))
            continue
        if eff < required:
            entry.update(eligible=False,
                         rejected=f"effective confidence {eff:.2f} < required "
                                  f"{required:.2f} for a {iv.type}")
            candidates.append((None, entry))
            continue
        entry["eligible"] = True
        candidates.append((iv, entry))

    eligible = [(iv, e) for iv, e in candidates if iv is not None]

    if not eligible:
        outcome.intervention_id, outcome.savable = None, False
        outcome.mode = "defer"
        outcome.rationale = f"no authorized intervention matches {outcome.reason}"
        outcome.decision_trace = [e for _, e in candidates]
        return outcome

    # 4. Select. "preferred" = curated order (safe default); "expected_value" = maximize EV.
    if policy.rank_by == "expected_value":
        chosen, chosen_entry = max(eligible, key=lambda t: (t[1]["expected_value"], -t[1]["rank"]))
        method = "max expected_value"
    else:
        chosen, chosen_entry = eligible[0]
        method = "preferred order"

    chosen_entry["chosen"] = True
    outcome.intervention_id = chosen.id
    outcome.mode = _mode(eff, policy)
    outcome.economics["margin_spent"] = chosen_entry["cost"]
    outcome.economics["chosen_expected_value"] = chosen_entry["expected_value"]
    outcome.rationale = (f"{outcome.reason} -> {chosen.type}: {chosen.description} "
                         f"[{method}; EV={chosen_entry['expected_value']}, "
                         f"margin={chosen_entry['cost']}; {outcome.mode}]")
    outcome.decision_trace = [e for _, e in candidates]
    return outcome


def _mode(effective_confidence: float, policy: Policy) -> str:
    """Above the act bar we're confident enough to auto-apply; between floor and act we
    only recommend (the company/ops decides); below floor we've already deferred."""
    return "act" if effective_confidence >= policy.act_confidence else "suggest"


def _corroborate(outcome: Outcome, policy: Policy, user: UserContext) -> dict:
    """Check the diagnosis against the behavioral data. Returns a declared verdict:
    corroborated / contradicted / unverified (no rule for this reason)."""
    rule = policy.corroboration.get(outcome.reason)
    if not rule:
        return {"status": "unverified", "rule": None}
    ok = _eval_rule(rule, _scope(outcome, user))
    return {"status": "corroborated" if ok else "contradicted", "rule": rule}


def _scope(outcome: Outcome, user: UserContext) -> dict:
    """The fields a rule (eligible_when or corroboration) may reference."""
    return {"reason": outcome.reason, "confidence": outcome.confidence,
            "tenure": user.tenure_days, "mrr": user.mrr,
            "activated": user.activated, "logins": user.logins_last_30d,
            **user.signals}


def _eligible(iv: Intervention, outcome: Outcome, user: UserContext, policy: Policy) -> bool:
    # A discount is only ever legitimate for reasons the company authorized it for.
    if iv.type == "discount" and outcome.reason not in policy.discount_reasons:
        return False
    if not iv.eligible_when:
        return True
    return _eval_rule(iv.eligible_when, _scope(outcome, user))


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
