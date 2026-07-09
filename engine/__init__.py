"""Offboard engine: the deterministic spine + the adaptive interviewer.

This package is the durable foundation. The Milestone 1 eval consumes it now;
Milestones 2 (API) and 3 (SDK) will consume the same modules unchanged.
"""

from .taxonomy import (
    COVER_STORIES,
    Intervention,
    Outcome,
    ProductConfig,
    Reason,
    UserContext,
)
from .interviewer import MAX_TURNS, MODEL, Interviewer
from .policy import CONFIDENCE_FLOOR, decide

__all__ = [
    "COVER_STORIES",
    "Reason",
    "Intervention",
    "ProductConfig",
    "UserContext",
    "Outcome",
    "Interviewer",
    "MAX_TURNS",
    "MODEL",
    "decide",
    "CONFIDENCE_FLOOR",
]
