"""Learning: close the flywheel loop. Reads the realized-save log (runs/resolutions.jsonl)
and PROPOSES updated economic priors for a human to approve -- it never auto-applies.

This is the safe, honest form of "the system learns": deterministic statistical estimation
over your own data, batch and reviewable -- not LLM retraining, and not a silent online loop
(which would be biased, since you only observe offers you actually made). See
docs/DECISIONING.md §7.
"""

from .experiment import (
    CellResult,
    JoinedSession,
    JoinReport,
    analyze,
    cells,
    join_sessions,
)
from .experiment import report as causal_report
from .recalibrate import (
    ProposedChange,
    RecalibrationProposal,
    aggregate,
    apply_proposal,
    load_resolutions,
    propose,
    report,
)

__all__ = [
    "load_resolutions",
    "aggregate",
    "propose",
    "apply_proposal",
    "report",
    "RecalibrationProposal",
    "ProposedChange",
    # Causal readout (the holdout half of the flywheel)
    "analyze",
    "join_sessions",
    "cells",
    "causal_report",
    "CellResult",
    "JoinedSession",
    "JoinReport",
]
