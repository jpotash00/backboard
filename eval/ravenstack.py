"""Data-grounded personas from the RavenStack SaaS dataset.

RavenStack (Kaggle: rivalytics/saas-subscription-and-churn-analytics-dataset) is a
5-table simulated SaaS business. We use it for one thing: *realistic customer context*.

Why not the labels. The dataset's `reason_code` and `feedback_text` are drawn from
INDEPENDENT random distributions -- a "features" churner is no likelier to have low feature
usage than anyone else. So the label carries no causal signal and can't be ground truth.
What IS usable is the behavior: MRR, tenure, per-feature usage, errors, support satisfaction.

So we invert the problem. We build a real `UserContext` from the joined tables, then DERIVE
the hidden reason FROM those signals (low usage -> never_activated; a usage cliff -> value_ended;
low satisfaction / errors -> product_quality; ...). That restores the behavior -> reason causal
link the synthetic data lacks, which is exactly what makes the interview testable. The churner's
`feedback_text` becomes the COVER STORY they lead with -- often at odds with the derived truth,
which is the whole point of the exit interview.

The result is a large, realistic persona pool that plugs straight into eval.run_eval, alongside
the ten hand-authored personas (which stay the curated baseline).

    from eval.ravenstack import build_ravenstack_personas
    personas = build_ravenstack_personas(n=30, seed=0)   # deterministic

Run `python -m eval.ravenstack` to print a summary without spending a single model call.
"""

import csv
import os
from collections import defaultdict
from datetime import datetime
from statistics import mean

from engine import UserContext
from .personas import Persona

# --- dataset location ---------------------------------------------------------------------

_FILES = ("accounts", "subscriptions", "feature_usage", "support_tickets", "churn_events")


def _dataset_dir(path: str | None = None) -> str:
    """The RavenStack CSV directory. Explicit `path` or $RAVENSTACK_DIR win; otherwise pull
    (cached) via kagglehub so the eval is reproducible on a fresh machine."""
    path = path or os.getenv("RAVENSTACK_DIR")
    if path:
        return path
    import kagglehub  # lazy: only needed when no local path is provided
    return kagglehub.dataset_download("rivalytics/saas-subscription-and-churn-analytics-dataset")


def _load(path: str | None = None) -> dict[str, list[dict]]:
    d = _dataset_dir(path)
    return {name: list(csv.DictReader(open(os.path.join(d, f"ravenstack_{name}.csv"))))
            for name in _FILES}


# --- small parse helpers ------------------------------------------------------------------

def _date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _flag(s) -> bool:
    return str(s).strip().lower() == "true"


# --- reason derivation (behavior -> truth) ------------------------------------------------

# First-choice intervention TYPE policy should authorize for each derived reason, given ACME's
# menu. Mirrors engine.taxonomy._default_preferred so the eval scores the intended save.
REASON_TO_EXPECTED = {
    "never_activated":     "onboarding",
    "product_quality":     "support",
    "value_ended":         "pause",
    "missing_capability":  "roadmap",
    "switched_competitor": "discount",
    "price_value_mismatch": "discount",
}

# feedback_text -> the cover-story label + how the churner opens.
_COVER = {
    "too expensive":         ("too_expensive", "Honestly, it's just gotten too expensive."),
    "missing features":      ("missing_features", "It's missing features we really need."),
    "switched to competitor": ("switched_competitor", "We're moving over to another tool."),
    "":                      ("not_using_it", "We're just not really using it anymore."),
}


def _derive_reason(ctx: UserContext, sig: dict, feedback: str) -> str:
    """The hidden truth, caused by the behavior. Priority order matters: the strongest
    behavioral tell wins, and only then do we fall back to the self-reported feedback."""
    used_features = sig["distinct_features"]
    total_usage = sig["total_usage"]
    last30 = sig["usage_last_30d"]
    satisfaction = sig["avg_satisfaction"]        # None when no tickets
    escalations = sig["escalations"]
    errors = sig["total_errors"]

    # 1. Barely adopted at all -> never got value. The clearest behavioral signature.
    if not ctx.activated:
        return "never_activated"
    # 2. Real pain: low CSAT, an escalation, or a pile of errors -> quality/support.
    if (satisfaction is not None and satisfaction < 3.0) or escalations > 0 or errors >= 6:
        return "product_quality"
    # 3. Was engaged, then a cliff to ~nothing -> the need ended, not a product problem.
    if total_usage >= 40 and last30 <= 2:
        return "value_ended"
    # 4. They named a competitor and were actually using it -> genuine switch.
    if feedback == "switched to competitor":
        return "switched_competitor"
    # 5. They named missing features and were engaged enough to know -> capability gap.
    if feedback == "missing features":
        return "missing_capability"
    # 6. Active, no dissatisfaction, still leaving -> it's the price/value at their stage.
    return "price_value_mismatch"


# --- persona construction -----------------------------------------------------------------

def _account_signals(acc, sub, usage_rows, tickets, churn) -> tuple[UserContext, dict]:
    churn_date = _date(churn["churn_date"])
    # Real customer tenure = account signup -> churn (not the last subscription's span, which can
    # be a day for a re-subscribed account). Fall back to the subscription window if either is missing.
    signup = _date(acc["signup_date"])
    start = _date(sub["start_date"])
    end = _date(sub["end_date"]) or churn_date
    if signup and churn_date:
        tenure_days = max(1, (churn_date - signup).days)
    else:
        tenure_days = max(1, (end - start).days) if (start and end) else 0

    counts = [int(_num(u["usage_count"])) for u in usage_rows]
    total_usage = sum(counts)
    distinct_features = len({u["feature_name"] for u in usage_rows})
    total_errors = sum(int(_num(u["error_count"])) for u in usage_rows)
    # Activity in the 30 days before churn: our proxy for logins_last_30d.
    last30 = 0
    for u in usage_rows:
        ud = _date(u["usage_date"])
        if ud and churn_date and 0 <= (churn_date - ud).days <= 30:
            last30 += int(_num(u["usage_count"]))

    csat_vals = [_num(t["satisfaction_score"]) for t in tickets if str(t["satisfaction_score"]).strip()]
    avg_csat = round(mean(csat_vals), 1) if csat_vals else None
    escalations = sum(_flag(t["escalation_flag"]) for t in tickets)

    # Activated = actually adopted: touched a couple of features with real volume.
    activated = distinct_features >= 3 and total_usage >= 30

    mrr = round(_num(sub["mrr_amount"]))
    plan = sub["plan_tier"] or acc["plan_tier"]
    seats = int(_num(sub["seats"] or acc["seats"]))

    sig = {
        "distinct_features": distinct_features, "total_usage": total_usage,
        "usage_last_30d": last30, "total_errors": total_errors,
        "avg_satisfaction": avg_csat, "escalations": escalations,
        "support_tickets": len(tickets), "seats": seats,
        "industry": acc["industry"], "refund_usd": round(_num(churn["refund_amount_usd"]), 2),
        "downgrade_before_churn": _flag(churn["preceding_downgrade_flag"]),
    }
    usage_summary = (
        f"{plan} plan, {seats} seats, {tenure_days}d tenure. "
        f"Used {distinct_features} features, {total_usage} total actions "
        f"({last30} in the last 30d). {len(tickets)} support tickets"
        + (f", avg CSAT {avg_csat}/5" if avg_csat is not None else "")
        + (f", {escalations} escalated" if escalations else "")
        + (f", {total_errors} errors" if total_errors else "") + "."
    )
    ctx = UserContext(
        user_id=acc["account_id"], plan=plan, mrr=mrr, tenure_days=tenure_days,
        logins_last_30d=min(last30, tenure_days), activated=activated,
        usage_summary=usage_summary, signals=sig,
    )
    return ctx, sig


def _persona_from_account(pid: int, acc, sub, usage_rows, tickets, churn) -> Persona | None:
    ctx, sig = _account_signals(acc, sub, usage_rows, tickets, churn)
    feedback = (churn["feedback_text"] or "").strip()
    reason = _derive_reason(ctx, sig, feedback)
    expected = REASON_TO_EXPECTED.get(reason)
    if expected is None:
        return None
    cover_label, opening = _COVER.get(feedback, _COVER[""])

    # The roleplay ground truth: the churner knows the derived reason and the real facts.
    hidden_desc = {
        "never_activated": "You signed up but never really got it running -- you barely touched "
                           "the product and never felt the value. 'Too expensive' is just the easy thing to say.",
        "product_quality": "It's been buggy/frustrating and support let you down; that's the real "
                          "reason, even if you lead with something softer.",
        "value_ended": "You used it hard for a while, then the need dried up. Nothing wrong with "
                       "the product -- the use case is simply over for now.",
        "missing_capability": "You genuinely use it, but it's missing a capability your team needs, "
                             "and that gap is why you're leaving.",
        "switched_competitor": "You've picked a competitor that fits better; the switch is already "
                              "underway. You'll admit it if asked directly.",
        "price_value_mismatch": "You get real value and use it regularly, but the price doesn't "
                               "pencil out at your stage. It IS about money -- but earned, not a cover.",
    }[reason]
    personality = (
        f"A churning {sig['industry']} customer on the {ctx.plan} plan (${ctx.mrr}/mo, "
        f"{ctx.tenure_days}d). Terse, a little hurried. Leads with \"{feedback or 'not using it'}\"."
    )
    # The tell is misleading when the stated feedback points at a different reason than the truth.
    feedback_reason = {"too expensive": "price_value_mismatch", "missing features": "missing_capability",
                       "switched to competitor": "switched_competitor"}.get(feedback)
    misleading = feedback_reason is not None and feedback_reason != reason

    return Persona(
        id=pid, hidden_reason=reason, cover_story=cover_label, opening_line=opening,
        personality=personality, hidden_description=hidden_desc, user=ctx,
        expected_intervention_type=expected, misleading_tell=misleading,
    )


def build_ravenstack_personas(n: int = 30, seed: int = 0, path: str | None = None) -> list[Persona]:
    """Deterministic, data-grounded personas. Joins the churn events to their account,
    churned subscription, feature usage and support history, then emits `n` personas sampled
    evenly across the derived reasons (so the eval isn't dominated by one bucket)."""
    t = _load(path)
    subs_by_acct = defaultdict(list)
    for s in t["subscriptions"]:
        subs_by_acct[s["account_id"]].append(s)
    usage_by_sub = defaultdict(list)
    for u in t["feature_usage"]:
        usage_by_sub[u["subscription_id"]].append(u)
    tickets_by_acct = defaultdict(list)
    for tk in t["support_tickets"]:
        tickets_by_acct[tk["account_id"]].append(tk)
    accounts = {a["account_id"]: a for a in t["accounts"]}

    built: list[Persona] = []
    pid = 0
    for churn in t["churn_events"]:
        acc = accounts.get(churn["account_id"])
        if not acc:
            continue
        subs = subs_by_acct.get(churn["account_id"], [])
        # The subscription that actually churned (else the latest one for the account).
        churned = [s for s in subs if _flag(s["churn_flag"])] or subs
        if not churned:
            continue
        sub = max(churned, key=lambda s: _date(s["start_date"]) or datetime.min)
        # A save offer only makes sense for a paying churner. Trials and $0 rows can't have a
        # price objection and there's no MRR to retain, so they're not eval material.
        if _flag(sub["is_trial"]) or _num(sub["mrr_amount"]) <= 0:
            continue
        usage_rows = usage_by_sub.get(sub["subscription_id"], [])
        pid += 1
        p = _persona_from_account(pid, acc, sub, usage_rows, tickets_by_acct.get(acc["account_id"], []), churn)
        if p is not None:
            built.append(p)

    # Even sample across reasons for a balanced eval, deterministic under `seed`.
    import random
    rng = random.Random(seed)
    by_reason: dict[str, list[Persona]] = defaultdict(list)
    for p in built:
        by_reason[p.hidden_reason].append(p)
    for bucket in by_reason.values():
        rng.shuffle(bucket)
    out: list[Persona] = []
    reasons = sorted(by_reason)
    i = 0
    while len(out) < min(n, len(built)):
        bucket = by_reason[reasons[i % len(reasons)]]
        if bucket:
            out.append(bucket.pop())
        i += 1
        if all(not b for b in by_reason.values()):
            break
    for j, p in enumerate(out, 1):   # renumber 1..n after sampling
        p.id = j
    return out


def churner_population(path: str | None = None) -> list[dict]:
    """Every paying churned account as a lightweight economic record -- the NATURAL (unbalanced)
    distribution, for revenue simulation over the whole book rather than a balanced eval sample.
    Each record: {account_id, reason, mrr, plan, tenure_days, expected_offer}."""
    t = _load(path)
    subs_by_acct = defaultdict(list)
    for s in t["subscriptions"]:
        subs_by_acct[s["account_id"]].append(s)
    usage_by_sub = defaultdict(list)
    for u in t["feature_usage"]:
        usage_by_sub[u["subscription_id"]].append(u)
    tickets_by_acct = defaultdict(list)
    for tk in t["support_tickets"]:
        tickets_by_acct[tk["account_id"]].append(tk)
    accounts = {a["account_id"]: a for a in t["accounts"]}

    out: list[dict] = []
    for churn in t["churn_events"]:
        acc = accounts.get(churn["account_id"])
        if not acc:
            continue
        subs = subs_by_acct.get(churn["account_id"], [])
        churned = [s for s in subs if _flag(s["churn_flag"])] or subs
        if not churned:
            continue
        sub = max(churned, key=lambda s: _date(s["start_date"]) or datetime.min)
        if _flag(sub["is_trial"]) or _num(sub["mrr_amount"]) <= 0:
            continue
        ctx, sig = _account_signals(acc, sub, usage_by_sub.get(sub["subscription_id"], []),
                                    tickets_by_acct.get(acc["account_id"], []), churn)
        reason = _derive_reason(ctx, sig, (churn["feedback_text"] or "").strip())
        out.append({"account_id": acc["account_id"], "reason": reason, "mrr": ctx.mrr,
                    "plan": ctx.plan, "tenure_days": ctx.tenure_days,
                    "expected_offer": REASON_TO_EXPECTED[reason]})
    return out


if __name__ == "__main__":
    import collections
    ps = build_ravenstack_personas(n=int(os.getenv("N", "30")))
    print(f"Built {len(ps)} data-grounded personas from RavenStack\n")
    dist = collections.Counter(p.hidden_reason for p in ps)
    print("derived reason distribution:")
    for r, c in dist.most_common():
        print(f"  {r:22} {c}   -> expected offer: {REASON_TO_EXPECTED[r]}")
    mis = sum(p.misleading_tell for p in ps)
    print(f"\nmisleading cover story (feedback != truth): {mis}/{len(ps)}")
    print("\nsample personas:")
    for p in ps[:5]:
        u = p.user
        print(f"  #{p.id} {p.hidden_reason:20} cover=\"{p.cover_story}\"  "
              f"{u.plan} ${u.mrr}/mo {u.tenure_days}d  act={u.activated}")
        print(f"      {u.usage_summary}")
