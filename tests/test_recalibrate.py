"""The recalibration job: propose updated priors from realized saves. Pure functions over
synthetic resolution records -- no API, no model, no files (except the load test)."""

import json

from engine import Scoring
from learning import (
    aggregate,
    apply_proposal,
    load_resolutions,
    propose,
    report,
)


def cell(reason, itype, offered, accepted):
    """`offered` resolution records for one cell, `accepted` of them accepted."""
    return [{"offered": True, "reason": reason, "intervention_type": itype,
             "accepted": i < accepted} for i in range(offered)]


def _find(changes, key):
    return next(c for c in changes if c.key == key)


def test_aggregate_counts_offered_and_accepted():
    records = cell("never_activated", "onboarding", 10, 4)
    assert aggregate(records) == {("never_activated", "onboarding"): (10, 4)}


def test_load_filters_non_offers_and_bad_lines(tmp_path):
    p = tmp_path / "resolutions.jsonl"
    p.write_text("\n".join([
        json.dumps({"offered": True, "reason": "x", "intervention_type": "discount", "accepted": True}),
        json.dumps({"offered": False, "reason": "y", "intervention_type": "pause", "accepted": False}),  # no offer
        json.dumps({"offered": True, "reason": None, "intervention_type": "pause", "accepted": True}),   # no reason
        "{not json",
        "",
    ]))
    recs = load_resolutions(str(p))
    assert len(recs) == 1 and recs[0]["reason"] == "x"


def test_load_missing_file_is_empty():
    assert load_resolutions("does/not/exist.jsonl") == []


def test_save_prior_nudges_toward_observed():
    # never_activated offered onboarding: observed accept 0.40. current sp 0.35, eff 0.80.
    # raw sp = 0.40 / 0.80 = 0.50; shrunk (100*0.50 + 20*0.35)/120 = 0.475.
    prop = propose(cell("never_activated", "onboarding", 100, 40), Scoring())
    c = _find(prop.save_prior, "never_activated")
    assert c.changed and c.observed_rate == 0.4 and c.n_offered == 100
    assert c.current == 0.35 and c.proposed == 0.475


def test_effectiveness_estimate_and_clamp():
    prop = propose(cell("price_value_mismatch", "discount", 100, 40), Scoring())
    # eff discount: raw = 0.40 / 0.50(sp price) = 0.80; shrunk (100*0.8 + 20*0.9)/120 = 0.817.
    e = _find(prop.type_effectiveness, "discount")
    assert e.current == 0.9 and e.proposed == 0.817 and e.changed
    # a cell whose implied factor exceeds 1 is clamped, and says so.
    prop2 = propose(cell("never_activated", "onboarding", 100, 40), Scoring())
    e2 = _find(prop2.type_effectiveness, "onboarding")
    assert e2.proposed <= 1.0 and "clamped" in e2.note


def test_min_samples_gates_thin_cells():
    prop = propose(cell("value_ended", "pause", 5, 2), Scoring(), min_samples=30)
    c = _find(prop.save_prior, "value_ended")
    assert not c.changed and c.proposed == c.current and "insufficient" in c.note


def test_shrinkage_pulls_small_n_toward_current():
    # Same observed rate, different sample sizes: more data -> closer to the raw estimate.
    small = _find(propose(cell("never_activated", "onboarding", 40, 16), Scoring()).save_prior,
                  "never_activated")   # rate 0.40, n=40
    big = _find(propose(cell("never_activated", "onboarding", 4000, 1600), Scoring()).save_prior,
                "never_activated")     # rate 0.40, n=4000
    current, raw = 0.35, 0.5
    assert abs(small.proposed - current) < abs(big.proposed - current)
    assert abs(big.proposed - raw) < abs(small.proposed - raw)


def test_apply_is_pure_and_only_changes_confident_cells():
    scoring = Scoring()
    records = cell("never_activated", "onboarding", 100, 40) + cell("value_ended", "pause", 5, 2)
    prop = propose(records, scoring)
    updated = apply_proposal(prop, scoring)
    assert updated.save_probability("never_activated") == 0.475   # changed
    assert updated.save_probability("value_ended") == scoring.save_probability("value_ended")  # gated
    # original Scoring untouched
    assert scoring.save_probability("never_activated") == 0.35


def test_report_runs_and_surfaces_the_bias_caveat():
    prop = propose(cell("never_activated", "onboarding", 100, 40), Scoring())
    text = report(prop)
    assert "RECALIBRATION PROPOSAL" in text
    assert "holdout" in text.lower() and "observational" in text.lower()
    assert "never_activated" in text
