"""Session state store. In-memory for Milestone 2; the interface is deliberately
Redis-shaped (create/get/save) so swapping the backend later is a drop-in.

A live interview holds an `Interviewer` (which carries the model client and message
history), so state is process-local for now. `SessionState.snapshot()` / `restore()` are the
serialization seam a Redis backend needs: snapshot the transcript + messages + turn count,
and reconstruct the Interviewer per request (config comes from the registry, the model
client is injected). Sessions also carry a TTL so an in-memory store can't grow without
bound -- an abandoned cancel flow is evicted rather than pinned in memory forever.
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from engine import Interviewer, Outcome, ProductConfig, UserContext

DEFAULT_TTL_SECONDS = 30 * 60


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
    # Experiment arm: "treatment" (offer served) or "control" (diagnosed, offer withheld).
    # Assigned once at session start; see api.experiment.
    arm: str = "treatment"
    # The intervention policy WOULD have served, computed regardless of arm. For control this
    # is the counterfactual offer (never shown); it makes the two arms comparable per cell.
    intended_intervention_id: Optional[str] = None

    def snapshot(self) -> dict:
        """Serialize everything except the live Interviewer and the model client. The
        interviewer's replayable state (message history + turn count) is captured so it can be
        rebuilt by `restore`. This is what a Redis/DB backend persists per session."""
        return {
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "user": asdict(self.user),
            "messages": list(self.interviewer.messages),
            "turns": self.interviewer.turns,
            "transcript": self.transcript,
            "outcome": asdict(self.outcome) if self.outcome else None,
            "closing_message": self.closing_message,
            "done": self.done,
            "resolution": self.resolution,
            "arm": self.arm,
            "intended_intervention_id": self.intended_intervention_id,
        }


def restore(data: dict, config: ProductConfig, client) -> SessionState:
    """Rebuild a SessionState from `snapshot()` output, re-attaching the config (from the
    registry) and a fresh model client. The Interviewer is reconstructed and its replayable
    state re-hydrated -- no interview context is lost across a process/host boundary."""
    user = UserContext(**data["user"])
    interviewer = Interviewer(config, user, client=client)
    interviewer.messages = list(data["messages"])
    interviewer.turns = int(data["turns"])
    return SessionState(
        session_id=data["session_id"],
        customer_id=data["customer_id"],
        interviewer=interviewer,
        config=config,
        user=user,
        transcript=data.get("transcript", []),
        outcome=Outcome(**data["outcome"]) if data.get("outcome") else None,
        closing_message=data.get("closing_message"),
        done=data.get("done", False),
        resolution=data.get("resolution"),
        arm=data.get("arm", "treatment"),
        intended_intervention_id=data.get("intended_intervention_id"),
    )


class SessionStore:
    """Process-local store with TTL eviction. `now`/`ttl_seconds` are injectable so tests can
    drive expiry deterministically."""

    def __init__(
        self,
        now: Callable[[], float] = time.time,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._seen: dict[str, float] = {}
        self._now = now
        self._ttl = ttl_seconds

    def create(self, state: SessionState) -> None:
        self._sessions[state.session_id] = state
        self._seen[state.session_id] = self._now()

    def get(self, session_id: str) -> Optional[SessionState]:
        self._evict_expired()
        state = self._sessions.get(session_id)
        if state is not None:
            self._seen[session_id] = self._now()  # touch: keep active sessions alive
        return state

    def save(self, state: SessionState) -> None:
        # In-memory the object is already live; this keeps the Redis seam explicit.
        self._sessions[state.session_id] = state
        self._seen[state.session_id] = self._now()

    def _evict_expired(self) -> None:
        cutoff = self._now() - self._ttl
        stale = [sid for sid, seen in self._seen.items() if seen < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
            self._seen.pop(sid, None)
