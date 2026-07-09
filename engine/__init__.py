"""Offboard engine: the deterministic spine + the adaptive interviewer.

This package is the durable foundation. The Milestone 1 eval consumes it now;
Milestones 2 (API) and 3 (SDK) will consume the same modules unchanged.
"""

from .taxonomy import (
    COVER_STORIES,
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_REASONS,
    Intervention,
    Outcome,
    Policy,
    ProductConfig,
    Reason,
    ReasonDef,
    UserContext,
)
from .interviewer import MAX_TURNS, MODEL, Interviewer
from .policy import CONFIDENCE_FLOOR, decide
from .serialization import (
    config_from_dict,
    config_from_json,
    config_to_dict,
    config_to_json,
)

__all__ = [
    "COVER_STORIES",
    "DEFAULT_CONFIDENCE_FLOOR",
    "DEFAULT_REASONS",
    "Reason",
    "ReasonDef",
    "Intervention",
    "Policy",
    "ProductConfig",
    "UserContext",
    "Outcome",
    "Interviewer",
    "MAX_TURNS",
    "MODEL",
    "decide",
    "CONFIDENCE_FLOOR",
    "config_to_dict",
    "config_from_dict",
    "config_to_json",
    "config_from_json",
]
