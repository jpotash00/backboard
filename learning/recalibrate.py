"""Propose updated policy priors from the realized-save log.

The engine prices a save as EV = save_prior[reason] * effectiveness[type] * value - cost.
`resolutions.jsonl` records, per offer made, the (reason, intervention_type) and whether the
user accepted. The observed accept rate for a cell estimates the PRODUCT
save_prior[reason] * effectiveness[type].

To keep this honest and identifiable, we do NOT attempt a joint factorization (the product is
identified from data, the split is not). Instead each factor is nudged from its own marginal,
holding the other at its current value -- a conservative one-step estimator:

    save_prior[r]      <- weighted_avg_t( accept_rate(r,t) / current_effectiveness[t] )
    effectiveness[t]   <- weighted_avg_r( accept_rate(r,t) / current_save_prior[r] )

each then shrunk toward the current value by sample size, and only proposed once a cell clears
`min_samples`. Nothing is applied automatically -- `propose()` returns a reviewable diff;
`apply_proposal()` builds an updated Scoring only when a human decides to.

IMPORTANT caveat, surfaced in every report: these rates are OBSERVATIONAL. You only see
outcomes for offers you actually made, so the estimates are selection-biased. For causal
effect (and to learn about offers policy currently withholds) pair this with a holdout arm.
"""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from engine import Scoring

DEFAULT_MIN_SAMPLES = 30
DEFAULT_SHRINKAGE = 20.0   # pseudo-observations pulling a proposal toward the current value


def load_resolutions(path: str = "runs/resolutions.jsonl") -> list[dict]:
    """Read resolution records where an offer was actually made (offered == True)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("offered") and rec.get("reason") and rec.get("intervention_type"):
            out.append(rec)
    return out


def aggregate(records: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    """(reason, intervention_type) -> (offered, accepted)."""
    cells: dict[tuple[str, str], list[int]] = {}
    for rec in records:
        key = (rec["reason"], rec["intervention_type"])
        c = cells.setdefault(key, [0, 0])
        c[0] += 1
        c[1] += 1 if rec.get("accepted") else 0
    return {k: (v[0], v[1]) for k, v in cells.items()}


@dataclass
class ProposedChange:
    key: str                 # a reason id or an intervention type
    current: float
    proposed: float
    n_offered: int
    n_accepted: int
    changed: bool            # False when kept at current (insufficient data)
    note: str = ""

    @property
    def observed_rate(self) -> Optional[float]:
        return round(self.n_accepted / self.n_offered, 3) if self.n_offered else None


@dataclass
class RecalibrationProposal:
    save_prior: list[ProposedChange] = field(default_factory=list)
    type_effectiveness: list[ProposedChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    min_samples: int = DEFAULT_MIN_SAMPLES

    @property
    def has_changes(self) -> bool:
        return any(c.changed for c in self.save_prior + self.type_effectiveness)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _shrink(raw: float, current: float, n: int, k: float) -> float:
    return round((n * raw + k * current) / (n + k), 3)


def propose(
    records: list[dict],
    scoring: Scoring,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    shrinkage: float = DEFAULT_SHRINKAGE,
) -> RecalibrationProposal:
    cells = aggregate(records)
    proposal = RecalibrationProposal(min_samples=min_samples)

    reasons = sorted({r for r, _ in cells})
    types = sorted({t for _, t in cells})

    # save_prior[r]: back out reason saveability using CURRENT effectiveness per offered type.
    for r in reasons:
        current = scoring.save_probability(r)
        n = acc = 0
        weighted = 0.0
        clamped = False
        for (rr, t), (off, a) in cells.items():
            if rr != r:
                continue
            n += off
            acc += a
            eff = scoring.effectiveness_of(t) or 1e-6
            raw_cell = (a / off) / eff
            weighted += off * raw_cell
        if n < min_samples:
            proposal.save_prior.append(ProposedChange(
                r, current, current, n, acc, changed=False,
                note=f"insufficient data (n={n} < {min_samples})"))
            continue
        raw = weighted / n
        if raw > 1.0:
            clamped = True
        proposed = _shrink(_clamp01(raw), current, n, shrinkage)
        note = "clamped (implied rate > current effectiveness)" if clamped else ""
        proposal.save_prior.append(ProposedChange(
            r, current, proposed, n, acc, changed=(proposed != current), note=note))

    # effectiveness[t]: back out type effectiveness using CURRENT save_prior per reason.
    for t in types:
        current = scoring.effectiveness_of(t)
        n = acc = 0
        weighted = 0.0
        clamped = False
        for (r, tt), (off, a) in cells.items():
            if tt != t:
                continue
            n += off
            acc += a
            sp = scoring.save_probability(r) or 1e-6
            raw_cell = (a / off) / sp
            weighted += off * raw_cell
        if n < min_samples:
            proposal.type_effectiveness.append(ProposedChange(
                t, current, current, n, acc, changed=False,
                note=f"insufficient data (n={n} < {min_samples})"))
            continue
        raw = weighted / n
        if raw > 1.0:
            clamped = True
        proposed = _shrink(_clamp01(raw), current, n, shrinkage)
        note = "clamped (implied rate > current save_prior)" if clamped else ""
        proposal.type_effectiveness.append(ProposedChange(
            t, current, proposed, n, acc, changed=(proposed != current), note=note))

    proposal.warnings.append(
        "Rates are OBSERVATIONAL (only offers that were made). Estimates are "
        "selection-biased; pair with a holdout arm for causal signal before trusting large moves."
    )
    thin = [c.key for c in proposal.save_prior + proposal.type_effectiveness if not c.changed]
    if thin:
        proposal.warnings.append(f"Kept at current (too little data): {', '.join(sorted(set(thin)))}.")
    return proposal


def apply_proposal(proposal: RecalibrationProposal, scoring: Scoring) -> Scoring:
    """Return a NEW Scoring with the CHANGED proposals applied. Pure -- the caller decides to
    use it (after review). Unchanged cells keep their current values."""
    save_prior = dict(scoring.save_prior)
    for c in proposal.save_prior:
        if c.changed:
            save_prior[c.key] = c.proposed
    effectiveness = dict(scoring.type_effectiveness)
    for c in proposal.type_effectiveness:
        if c.changed:
            effectiveness[c.key] = c.proposed
    return replace(scoring, save_prior=save_prior, type_effectiveness=effectiveness)


def report(proposal: RecalibrationProposal) -> str:
    lines = ["RECALIBRATION PROPOSAL (review before applying)", "=" * 52]

    def block(title: str, changes: list[ProposedChange]) -> None:
        lines.append(f"\n{title}")
        lines.append(f"  {'key':<22}{'current':>9}{'proposed':>10}{'obs.rate':>10}{'n':>7}  note")
        for c in sorted(changes, key=lambda c: (not c.changed, c.key)):
            arrow = "->" if c.changed else "  "
            rate = "-" if c.observed_rate is None else f"{c.observed_rate:.2f}"
            lines.append(
                f"  {c.key:<22}{c.current:>9.3f}{c.proposed:>9.3f}{arrow}{rate:>9}"
                f"{c.n_offered:>7}  {c.note}")

    block("save_prior (per reason)", proposal.save_prior)
    block("type_effectiveness (per intervention type)", proposal.type_effectiveness)
    lines.append("")
    for w in proposal.warnings:
        lines.append(f"  ! {w}")
    lines.append("\n  Apply with learning.apply_proposal(proposal, scoring) once approved.")
    return "\n".join(lines)


def main() -> int:
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "runs/resolutions.jsonl"
    records = load_resolutions(path)
    if not records:
        print(f"No offered-resolution records in {path} yet -- nothing to recalibrate from.")
        return 0
    proposal = propose(records, Scoring())
    print(report(proposal))
    print(f"\n  ({len(records)} offers analyzed from {path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
