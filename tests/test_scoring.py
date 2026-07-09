"""Scoring math + the dropdown baseline. Pure functions, no API."""

from eval.scoring import Result, baseline_reason, crux_split_ok, score


def r(pid, hidden, cover, diagnosed, conf=0.9, turns=1, iv="x", exp="x",
      misleading=False, confirmer=False):
    return Result(
        persona_id=pid, hidden_reason=hidden, cover_story=cover,
        diagnosed_reason=diagnosed, confidence=conf, turns_used=turns,
        intervention_type=iv, expected_intervention_type=exp,
        misleading_tell=misleading, false_confirmer=confirmer,
    )


def test_baseline_believes_the_cover_story():
    assert baseline_reason("too_expensive") == "price_value_mismatch"
    assert baseline_reason("not_using_it") == "value_ended"
    assert baseline_reason("no_reason_given") == "unknown"


def test_trap_detection():
    # too_expensive naively -> price; a never_activated person is a trap.
    assert r(1, "never_activated", "too_expensive", "never_activated").is_cover_story_trap
    # too_expensive naively -> price; a real price churner is NOT a trap.
    assert not r(2, "price_value_mismatch", "too_expensive", "price_value_mismatch").is_cover_story_trap


def test_diagnostic_accuracy_counts_exact_matches():
    results = [
        r(1, "never_activated", "too_expensive", "never_activated"),
        r(2, "price_value_mismatch", "too_expensive", "value_ended"),  # wrong
    ]
    s = score(results)
    assert s.diagnostic_accuracy == 0.5


def test_penetration_only_over_traps():
    results = [
        # trap, got it right
        r(1, "never_activated", "too_expensive", "never_activated"),
        # trap, got it wrong
        r(5, "missing_capability", "too_expensive", "price_value_mismatch"),
        # NOT a trap (cover story matches truth) -- excluded from penetration
        r(2, "price_value_mismatch", "too_expensive", "value_ended"),
    ]
    s = score(results)
    assert s.n_traps == 2
    assert s.cover_story_penetration == 0.5  # 1 of 2 traps
    # baseline believes the cover story -> gets both traps wrong by construction
    assert s.baseline_cover_story_penetration == 0.0


def test_calibration_and_confident_wrong():
    results = [
        r(1, "never_activated", "too_expensive", "never_activated", conf=0.9),   # right, confident
        r(2, "value_ended", "not_using_it", "price_value_mismatch", conf=0.8),   # wrong, confident (danger)
        r(3, "product_quality", "not_using_it", "value_ended", conf=0.3),        # wrong, low conf (honest)
    ]
    s = score(results)
    assert s.mean_conf_correct == 0.9
    assert abs(s.mean_conf_wrong - 0.55) < 1e-9
    assert s.confident_and_wrong == 1  # only the conf=0.8 wrong one


def test_intervention_correctness():
    results = [
        r(1, "never_activated", "too_expensive", "never_activated", iv="onboarding", exp="onboarding"),
        r(2, "price_value_mismatch", "too_expensive", "price_value_mismatch", iv=None, exp="discount"),
    ]
    s = score(results)
    assert s.intervention_correctness == 0.5


def test_misleading_tell_accuracy_scored_separately():
    results = [
        # misleading tell, recovered the truth anyway -> counts
        r(11, "product_quality", "not_using_it", "product_quality", misleading=True),
        # misleading tell, fell for the tell (diagnosed the corroborated wrong reason)
        r(12, "switched_competitor", "too_expensive", "price_value_mismatch", misleading=True),
        # honest tell -> excluded from the misleading-tell metric
        r(1, "never_activated", "too_expensive", "never_activated"),
    ]
    s = score(results)
    assert s.n_misleading == 2
    assert s.misleading_tell_accuracy == 0.5  # 1 of 2 misleading personas recovered
    # aggregate accuracy is higher than the misleading-tell slice -- the shortcut hides
    # in the aggregate but is exposed by the separate metric.
    assert s.diagnostic_accuracy > s.misleading_tell_accuracy


def test_false_confirmer_accuracy_scored_separately():
    results = [
        # acquiescent persona, interviewer stayed open and recovered the truth
        r(13, "missing_capability", "too_expensive", "missing_capability", confirmer=True),
        # acquiescent persona, interviewer led ("is it the price?") -> false confirmation
        r(14, "product_quality", "not_using_it", "value_ended", confirmer=True),
        # honest persona -> excluded from the anti-leading metric
        r(1, "never_activated", "too_expensive", "never_activated"),
    ]
    s = score(results)
    assert s.n_false_confirmer == 2
    assert s.false_confirmer_accuracy == 0.5  # 1 of 2; the led one was lost


def test_crux_split():
    good = [
        r(1, "never_activated", "too_expensive", "never_activated"),
        r(2, "price_value_mismatch", "too_expensive", "price_value_mismatch"),
    ]
    assert crux_split_ok(good) is True

    bad = [
        r(1, "never_activated", "too_expensive", "price_value_mismatch"),  # believed the cover story
        r(2, "price_value_mismatch", "too_expensive", "price_value_mismatch"),
    ]
    assert crux_split_ok(bad) is False

    assert crux_split_ok([r(1, "never_activated", "too_expensive", "never_activated")]) is None
