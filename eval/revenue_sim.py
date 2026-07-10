"""Revenue simulation: smart policy vs. discount-everyone vs. control.

WHAT THIS IS NOT. It is not the A/B result. RavenStack (like any churn dataset) has no
counterfactual -- no "what would this churner have done under a different offer" -- so save
effectiveness cannot be measured from data. Only a live holdout with real 90-day outcomes proves it.

WHAT THIS IS. A model. We assign plausible save-probabilities per (reason x offer), run three arms
over the whole paying-churner population, and report net retained MRR. The SAVE_PROB matrix below is
the assumption; edit it and re-run. It's an economics / pitch tool and a way to pressure-test the
thesis "the right offer beats a blanket discount on net revenue," not evidence for it.

    python -m eval.revenue_sim

Every number downstream is E[.] under SAVE_PROB, summed over the population.
"""

from collections import defaultdict

from .ravenstack import churner_population, REASON_TO_EXPECTED

# --- the assumption: P(save | reason, offer). Tune these. --------------------------------
SAVE_PROB = {
    "never_activated":      {"onboarding": 0.35, "discount": 0.10, "pause": 0.18, "none": 0.03},
    "price_value_mismatch": {"discount": 0.55, "downgrade": 0.45, "pause": 0.25, "none": 0.05},
    "value_ended":          {"pause": 0.40, "discount": 0.10, "none": 0.05},
    "missing_capability":   {"roadmap": 0.30, "discount": 0.15, "none": 0.05},
    "switched_competitor":  {"discount": 0.35, "roadmap": 0.20, "none": 0.05},
    "product_quality":      {"support": 0.40, "discount": 0.20, "none": 0.05},
}
# Fraction of MRR you KEEP when a save lands with this offer. A discount gives revenue up; an
# onboarding/support/roadmap save keeps full price; a pause keeps most of the annual value.
KEEP = {"onboarding": 1.0, "support": 1.0, "roadmap": 1.0, "downgrade": 0.55,
        "discount": 0.5, "pause": 0.75, "none": 1.0}

CONTROL_STAY = 0.05        # with NO offer, some churners stay anyway
DIAGNOSIS_ACCURACY = 0.90  # smart arm: how often it serves the RIGHT offer (≈ the eval's number)
NAIVE_OFFER = "discount"   # the naive baseline discounts everyone


def _p(reason, offer):
    return SAVE_PROB[reason].get(offer, 0.05)


def _arm(pop, offer_of):
    """Expected outcomes for an arm. `offer_of(rec)` returns (offer_type, save_prob, keep_frac).
    Returns totals: saved (E[count]), retained (E[MRR kept]), concession (E[MRR given up on saves])."""
    saved = retained = concession = 0.0
    for rec in pop:
        offer, p, keep = offer_of(rec)
        saved += p
        retained += p * rec["mrr"] * keep
        concession += p * rec["mrr"] * (1 - keep)
    return saved, retained, concession


def control_of(rec):
    return "none", CONTROL_STAY, KEEP["none"]


def naive_of(rec):
    return NAIVE_OFFER, _p(rec["reason"], NAIVE_OFFER), KEEP[NAIVE_OFFER]


def smart_of(rec):
    """Right offer for the reason, blended with diagnosis accuracy: a misdiagnosis serves an
    ill-fitting offer, modeled as the blanket discount's odds/economics."""
    right = rec["expected_offer"]
    pr, kr = _p(rec["reason"], right), KEEP[right]
    pw, kw = _p(rec["reason"], "discount"), KEEP["discount"]
    a = DIAGNOSIS_ACCURACY
    p = a * pr + (1 - a) * pw
    # effective keep = revenue-weighted so retained/concession stay consistent
    keep = (a * pr * kr + (1 - a) * pw * kw) / p if p else 1.0
    return right, p, keep


def run(path=None):
    pop = churner_population(path)
    at_risk = sum(r["mrr"] for r in pop)
    print(f"Population: {len(pop)} paying churned accounts, ${at_risk:,.0f}/mo at risk "
          f"(${at_risk*12:,.0f} ARR)\n")

    mix = defaultdict(int)
    for r in pop:
        mix[r["reason"]] += 1
    print("reason mix (derived from behavior):")
    for reason, c in sorted(mix.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:22} {c:4}  ({c/len(pop)*100:4.1f}%)  -> smart offer: {REASON_TO_EXPECTED[reason]}")

    arms = [("Control (no offer)", control_of),
            ("Naive (discount everyone)", naive_of),
            ("Smart (Offboard policy)", smart_of)]
    print(f"\n{'arm':28} {'saved':>7} {'save%':>6} {'retained MRR':>14} {'margin given up':>16} {'net/mo':>10}")
    print("-" * 88)
    rows = {}
    for name, fn in arms:
        saved, retained, concession = _arm(pop, fn)
        rows[name] = (saved, retained, concession)
        print(f"{name:28} {saved:7.0f} {saved/len(pop)*100:5.1f}% "
              f"${retained:12,.0f} ${concession:14,.0f} ${retained:8,.0f}")

    ctl = rows["Control (no offer)"][1]
    naive = rows["Naive (discount everyone)"]
    smart = rows["Smart (Offboard policy)"]
    print("\n--- vs. control (incremental retained MRR from intervening) ---")
    print(f"  Naive:  +${naive[1]-ctl:,.0f}/mo retained, giving up ${naive[2]:,.0f}/mo in discounts")
    print(f"  Smart:  +${smart[1]-ctl:,.0f}/mo retained, giving up ${smart[2]:,.0f}/mo in discounts")
    d_rev = smart[1] - naive[1]
    d_cost = naive[2] - smart[2]
    print(f"\n  Smart vs Naive: {'+' if d_rev>=0 else ''}${d_rev:,.0f}/mo retained "
          f"AND ${d_cost:,.0f}/mo LESS margin given up.")
    print(f"  Efficiency (retained per $1 of margin conceded):  "
          f"Naive ${naive[1]/naive[2]:.2f}  vs  Smart ${smart[1]/smart[2]:.2f}")
    print("\n(!) Illustrative under SAVE_PROB assumptions. The real numbers come from a live holdout.")


if __name__ == "__main__":
    run()
