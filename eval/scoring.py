"""The five scores (§5), as pure functions so they're testable without the API and
reusable as a regression suite when you iterate the interviewer prompt.

The dropdown baseline lives here too: the naive control that simply believes the
stated cover story. The DELTA between the interviewer and this baseline is the product.
"""

from dataclasses import dataclass
from typing import Optional

from engine import Reason

# The naive control: what reason a plain cancel-flow dropdown would infer if it just
# believed the stated cover story. This is the baseline the interviewer must beat.
COVER_TO_NAIVE_REASON: dict[str, Reason] = {
    "too_expensive":    "price_value_mismatch",
    "not_using_it":     "value_ended",
    "found_alternative": "switched_competitor",
    "missing_feature":  "missing_capability",
    "too_complicated":  "product_quality",
    "no_reason_given":  "unknown",
}


@dataclass
class Result:
    """One persona's run, flattened for scoring."""
    persona_id: int
    hidden_reason: Reason
    cover_story: str
    diagnosed_reason: str      # a config reason id -- open string, not the fixed enum
    confidence: float
    turns_used: int
    intervention_type: Optional[str]      # the type policy landed on, or None
    expected_intervention_type: str
    # True when the behavioral tell points AWAY from the truth (see Persona.misleading_tell).
    # Scored separately so the tell-reader shortcut can't hide inside the aggregate.
    misleading_tell: bool = False
    # True for acquiescent personas that confirm any label offered (see Persona.false_confirmer).
    # Accuracy here is a proxy for non-leading questioning discipline: lead them and you lose.
    false_confirmer: bool = False
    # The "believe-the-dashboard" adversary's diagnosis: what a strong classifier gets from
    # the snapshot + opener ALONE, with no interview (see eval.snapshot_only). None when the
    # adversary wasn't run. The interviewer's edge OVER this is what the conversation adds;
    # if they match, the label was recoverable from the shown features (feature leakage).
    snapshot_only_reason: Optional[str] = None

    @property
    def reason_correct(self) -> bool:
        return self.diagnosed_reason == self.hidden_reason

    @property
    def snapshot_only_correct(self) -> Optional[bool]:
        """Did the dashboard-only adversary get it right? None when it wasn't run."""
        if self.snapshot_only_reason is None:
            return None
        return self.snapshot_only_reason == self.hidden_reason

    @property
    def intervention_correct(self) -> bool:
        return self.intervention_type == self.expected_intervention_type

    @property
    def is_cover_story_trap(self) -> bool:
        """True when a dropdown that believed the cover story would get it WRONG.
        These are the personas the whole product exists to handle."""
        return COVER_TO_NAIVE_REASON.get(self.cover_story) != self.hidden_reason


def _rate(n: float | int, d: float | int) -> float:
    return n / d if d else 0.0


@dataclass
class Scores:
    diagnostic_accuracy: float
    cover_story_penetration: float
    intervention_correctness: float
    # Calibration: mean confidence when right vs wrong; the gap should be positive.
    mean_conf_correct: float
    mean_conf_wrong: float
    confident_and_wrong: int          # conf >= floor but reason wrong -- the danger cases
    mean_turns: float
    # Anti-telegraphing control: accuracy on the personas whose behavioral tell MISLEADS.
    # A model that just reads usage_summary craters here even if the aggregate looks fine.
    misleading_tell_accuracy: float
    # Anti-leading control: accuracy on acquiescent personas. Falls when the interviewer asks
    # leading/menu questions (they confirm the wrong label); a proxy for questioning bias.
    false_confirmer_accuracy: float
    # Baseline for the delta.
    baseline_diagnostic_accuracy: float
    baseline_cover_story_penetration: float
    # The second baseline: the believe-the-dashboard adversary (no interview). None when it
    # wasn't run. Where these match the interviewer, the conversation added nothing -- the
    # label was already sitting in the snapshot. The gap is the interview's real contribution.
    snapshot_only_diagnostic_accuracy: Optional[float]
    snapshot_only_cover_story_penetration: Optional[float]
    snapshot_only_misleading_tell_accuracy: Optional[float]
    n: int
    n_traps: int
    n_misleading: int
    n_false_confirmer: int
    n_snapshot_only: int


def baseline_reason(cover_story: str) -> Reason:
    """What the dropdown control diagnoses: it just believes the cover story."""
    return COVER_TO_NAIVE_REASON.get(cover_story, "unknown")


def score(results: list[Result], confidence_floor: float = 0.6) -> Scores:
    n = len(results)
    correct = [r for r in results if r.reason_correct]
    wrong = [r for r in results if not r.reason_correct]
    traps = [r for r in results if r.is_cover_story_trap]
    misleading = [r for r in results if r.misleading_tell]
    confirmers = [r for r in results if r.false_confirmer]

    conf_correct = [r.confidence for r in correct]
    conf_wrong = [r.confidence for r in wrong]

    # Second baseline: the believe-the-dashboard adversary. Only score it over personas
    # where it actually ran (snapshot_only_reason set), so a partial/absent run reads as
    # None rather than silently deflating to zero.
    snap = [r for r in results if r.snapshot_only_reason is not None]
    snap_traps = [r for r in snap if r.is_cover_story_trap]
    snap_mis = [r for r in snap if r.misleading_tell]

    return Scores(
        diagnostic_accuracy=_rate(len(correct), n),
        cover_story_penetration=_rate(
            sum(r.reason_correct for r in traps), len(traps)
        ),
        intervention_correctness=_rate(
            sum(r.intervention_correct for r in results), n
        ),
        mean_conf_correct=_rate(sum(conf_correct), len(conf_correct)),
        mean_conf_wrong=_rate(sum(conf_wrong), len(conf_wrong)),
        confident_and_wrong=sum(
            1 for r in wrong if r.confidence >= confidence_floor
        ),
        mean_turns=_rate(sum(r.turns_used for r in results), n),
        misleading_tell_accuracy=_rate(
            sum(r.reason_correct for r in misleading), len(misleading)
        ),
        false_confirmer_accuracy=_rate(
            sum(r.reason_correct for r in confirmers), len(confirmers)
        ),
        baseline_diagnostic_accuracy=_rate(
            sum(baseline_reason(r.cover_story) == r.hidden_reason for r in results), n
        ),
        baseline_cover_story_penetration=_rate(
            sum(baseline_reason(r.cover_story) == r.hidden_reason for r in traps),
            len(traps),
        ),
        snapshot_only_diagnostic_accuracy=(
            _rate(sum(r.snapshot_only_correct for r in snap), len(snap)) if snap else None
        ),
        snapshot_only_cover_story_penetration=(
            _rate(sum(r.snapshot_only_correct for r in snap_traps), len(snap_traps))
            if snap_traps else None
        ),
        snapshot_only_misleading_tell_accuracy=(
            _rate(sum(r.snapshot_only_correct for r in snap_mis), len(snap_mis))
            if snap_mis else None
        ),
        n=n,
        n_traps=len(traps),
        n_misleading=len(misleading),
        n_false_confirmer=len(confirmers),
        n_snapshot_only=len(snap),
    )


def crux_split_ok(results: list[Result]) -> Optional[bool]:
    """Personas 1 vs 2: identical cover story, opposite diagnosis. The whole thesis.
    Returns True/False, or None if either persona is missing from the results."""
    by_id = {r.persona_id: r for r in results}
    p1, p2 = by_id.get(1), by_id.get(2)
    if not p1 or not p2:
        return None
    return (
        p1.diagnosed_reason == "never_activated"
        and p2.diagnosed_reason == "price_value_mismatch"
    )
