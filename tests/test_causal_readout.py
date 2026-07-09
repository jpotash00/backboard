"""The decisive test: on a synthetic population with a KNOWN ground-truth lift, the causal
readout recovers the truth while the accept-rate estimator is fooled.

This is what makes the holdout more than scaffolding. We plant:
  - an ALWAYS-STAYER fraction `a` (retain regardless of arm -- the offer changes nothing), and
  - a per-offer TRUE save probability `s` for the swingable rest.
So the real causal lift is (1 - a) * s, and the real control retention is `a`. We then check:
  1. the causal readout recovers that lift (point estimate close, CI covers truth), and
  2. accept rate (learning.recalibrate's signal) massively overstates it -- and in the NULL
     case (s = 0, the offer does nothing) still reports a big fake save while the causal
     lift is ~0 and not significant.
"""

import random
from datetime import datetime, timedelta, timezone

from api.experiment import CONTROL, assign
from learning import aggregate, analyze
from learning.experiment import cells, join_sessions

HORIZON = 30
_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T0 = _BASE.isoformat()
_OBSERVED = (_BASE + timedelta(days=HORIZON + 1)).isoformat()  # matured past the horizon


def _simulate(n_users, always_stayer_rate, true_save_prob, accept_prob, seed):
    """Emit (resolutions, outcomes) in the exact shape the loggers write, with arms drawn by
    the real assign(). Returns the planted ground-truth lift too."""
    rng = random.Random(seed)
    resolutions, outcomes = [], []
    reason, itype = "price_value_mismatch", "discount"
    for i in range(n_users):
        uid = f"u{i}"
        arm = assign("exp", "acme", uid, holdout_fraction=0.5)
        always_stayer = rng.random() < always_stayer_rate

        if always_stayer:
            retained = True                      # retains in BOTH arms; offer irrelevant
        elif arm == CONTROL:
            retained = False                     # swingable, no offer -> churns
        else:
            retained = rng.random() < true_save_prob  # swingable, offer saves with prob s

        offered = arm != CONTROL
        accepted = offered and rng.random() < accept_prob
        resolutions.append({
            "logged_at": _T0, "customer_id": "acme", "user_id": uid, "mrr": 100.0,
            "arm": arm, "reason": reason, "mode": "suggest",
            "intervention_id": itype if offered else None,
            "intervention_type": itype if offered else None,
            "intended_intervention_id": itype, "intended_intervention_type": itype,
            "offered": offered, "accepted": accepted,
        })
        outcomes.append({
            "logged_at": _OBSERVED, "customer_id": "acme", "user_id": uid,
            "active": retained, "observed_at": _OBSERVED,
        })
    true_lift = (1 - always_stayer_rate) * true_save_prob
    return resolutions, outcomes, true_lift


def _cell(resolutions, outcomes):
    joined = join_sessions(resolutions, outcomes, HORIZON)
    (c,) = cells(joined.joined)
    return c, joined


def test_causal_recovers_known_lift_while_accept_rate_overstates():
    # Ground truth: 30% always-stayers, offer truly saves 50% of the swingable rest.
    res, out, true_lift = _simulate(
        n_users=6000, always_stayer_rate=0.30, true_save_prob=0.50,
        accept_prob=0.70, seed=1,
    )
    assert abs(true_lift - 0.35) < 1e-9

    c, joined = _cell(res, out)
    assert joined.pending == 0 and joined.unmatched == 0

    # 1. Causal lift lands on the truth, and its CI covers it.
    assert c.lift is not None and abs(c.lift - true_lift) < 0.04
    lo, hi = c.lift_ci()
    assert lo <= true_lift <= hi
    assert c.significant

    # Control retention recovers the always-stayer rate (~0.30), not zero.
    assert abs(c.r_control - 0.30) < 0.04

    # 2. Accept rate -- the OBSERVATIONAL estimator -- is ~0.70, nearly double the truth.
    #    It's what learning.recalibrate would feed into save_prior * effectiveness.
    ((offered, accepted),) = aggregate(
        [r for r in res if r["offered"]]
    ).values()
    naive = accepted / offered
    assert abs(naive - 0.70) < 0.04
    assert naive - c.lift > 0.25  # the bias the holdout removes


def test_null_offer_causal_reads_zero_but_accept_rate_still_lies():
    # The offer does NOTHING (true_save_prob = 0): true lift is 0. Accept rate still ~0.70
    # because people happily accept a free discount they didn't need.
    #
    # Significance on any SINGLE experiment is probabilistic -- a 95% test false-positives
    # ~5% of the time by construction. So we run many independent experiments and assert the
    # RIGHT properties: the causal lift centers on zero, false positives stay near 5%, and
    # accept rate reports a fat fake save EVERY time. That contrast is the whole point.
    lifts, false_positives, accept_rates = [], 0, []
    seeds = range(100, 140)
    for seed in seeds:
        res, out, true_lift = _simulate(
            n_users=4000, always_stayer_rate=0.30, true_save_prob=0.0,
            accept_prob=0.70, seed=seed,
        )
        assert true_lift == 0.0
        c, _ = _cell(res, out)
        lifts.append(c.lift)
        if c.significant:
            false_positives += 1
        ((offered, accepted),) = aggregate([r for r in res if r["offered"]]).values()
        accept_rates.append(accepted / offered)

    mean_lift = sum(lifts) / len(lifts)
    assert abs(mean_lift) < 0.01                       # causal read is unbiased at zero
    assert false_positives / len(lifts) <= 0.15        # ~5% expected; generous flake margin
    # Accept rate lies consistently: every experiment reports a ~0.70 "save" that isn't real.
    assert min(accept_rates) > 0.63
    assert sum(accept_rates) / len(accept_rates) > 0.68


def test_join_measures_at_the_fixed_horizon_not_before():
    # A user active at day 10 but cancelled by the horizon must count as CHURNED: retention is
    # read at/after the horizon, so pre-horizon "still here" doesn't rescue the row.
    early = (_BASE + timedelta(days=10)).isoformat()
    at_horizon = (_BASE + timedelta(days=HORIZON + 1)).isoformat()
    res = [{"logged_at": _T0, "customer_id": "acme", "user_id": "u1", "mrr": 50.0,
            "arm": "treatment", "reason": "price_value_mismatch",
            "intended_intervention_type": "discount", "offered": True, "accepted": True}]
    out = [
        {"customer_id": "acme", "user_id": "u1", "active": True, "observed_at": early},
        {"customer_id": "acme", "user_id": "u1", "active": False, "observed_at": at_horizon},
    ]
    joined = join_sessions(res, out, HORIZON)
    assert joined.pending == 0 and joined.unmatched == 0
    assert len(joined.joined) == 1
    assert joined.joined[0].retained is False        # horizon reading wins


def test_join_reports_pending_and_unmatched():
    # One session with only a pre-horizon reading (pending); one with no outcome at all
    # (unmatched). Neither is guessed -- both stay out of the readout.
    early = (_BASE + timedelta(days=5)).isoformat()
    res = [
        {"logged_at": _T0, "customer_id": "acme", "user_id": "pend", "mrr": 0.0,
         "arm": "treatment", "reason": "r", "intended_intervention_type": "discount",
         "offered": True, "accepted": True},
        {"logged_at": _T0, "customer_id": "acme", "user_id": "miss", "mrr": 0.0,
         "arm": "control", "reason": "r", "intended_intervention_type": "discount",
         "offered": False, "accepted": False},
    ]
    out = [{"customer_id": "acme", "user_id": "pend", "active": True, "observed_at": early}]
    joined = join_sessions(res, out, HORIZON)
    assert joined.joined == []
    assert joined.pending == 1
    assert joined.unmatched == 1


def test_net_value_can_go_negative_when_offer_does_nothing():
    # Same null offer, but now it's a costly discount. Net value must be negative: we give
    # away margin (on accepted offers) and buy no incremental retention.
    res, out, _ = _simulate(
        n_users=6000, always_stayer_rate=0.30, true_save_prob=0.0,
        accept_prob=0.70, seed=3,
    )
    c, _ = _cell(res, out)
    nv = c.net_value(value_horizon_months=12, cost=75.0)  # discount costs $75
    assert nv is not None and nv < 0
