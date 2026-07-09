"""Session state store. In-memory for Milestone 2; the interface is deliberately
Redis-shaped (create/get/save) so swapping the backend later is a drop-in.

A live interview holds an `Interviewer` (which carries the model client and message
history), so state is process-local for now. Redis will require serializing the
transcript + turn count and reconstructing the Interviewer per request.
"""

from dataclasses import dataclass, field
from typing import Optional

from engine import Interviewer, Outcome, ProductConfig, UserContext


@dataclass
class SessionState:
    session_id: str
    customer_id: str
    interviewer: Interviewer
    config: ProductConfig
    user: UserContext
    transcript: list[dict] = field(default_factory=list)  # [{speaker, text}]
    outcome: Optional[Outcome] = None
    closing_message: Optional[str] = None
    done: bool = False
    # What the user did with the offer: {"accepted": bool}. The flywheel's ground truth --
    # the realized save that recalibrates save_prior / effectiveness later.
    resolution: Optional[dict] = None


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def create(self, state: SessionState) -> None:
        self._sessions[state.session_id] = state

    def get(self, session_id: str) -> Optional[SessionState]:
        return self._sessions.get(session_id)

    def save(self, state: SessionState) -> None:
        # In-memory the object is already live; this keeps the Redis seam explicit.
        self._sessions[state.session_id] = state
