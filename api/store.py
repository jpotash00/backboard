"""Session state store. Two interchangeable backends behind one create/get/save interface:

  - `SessionStore`      -- process-local dict + TTL. Zero infra; single instance only.
  - `RedisSessionStore` -- shared/durable via Redis. Any instance behind a load balancer can
                           resume a session another started, and a redeploy doesn't drop
                           in-flight interviews. Selected when OFFBOARD_REDIS_URL is set.

A live interview holds an `Interviewer` (which carries the model client and message history),
which is not itself serializable. `SessionState.snapshot()` / `restore()` are the seam that
makes a shared backend possible: snapshot the transcript + messages + turn count, and
reconstruct the Interviewer per request (config comes from the registry, the model client is
injected). Sessions carry a TTL either way, so an abandoned cancel flow is evicted (in Redis,
via key expiry) rather than pinned forever.
"""

import json
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


class RedisSessionStore:
    """Shared, durable session store backed by Redis. Same create/get/save contract as
    `SessionStore`, so `create_app` swaps one for the other with no other change.

    Each session is stored as its JSON `snapshot()` under a TTL key; `get` rehydrates it via
    `restore()`, re-attaching the customer's config (looked up by the snapshot's `customer_id`)
    and a fresh model client. TTL is refreshed on read (sliding window), mirroring the
    in-memory store's keep-active-sessions behavior. Because state lives in Redis, not process
    memory, this is what unlocks multiple instances and zero-downtime deploys."""

    def __init__(
        self,
        redis_client,
        registry,
        client_factory: Callable[[], object],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        prefix: str = "offboard:session:",
    ) -> None:
        self._r = redis_client
        self._registry = registry
        self._client_factory = client_factory
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def _persist(self, state: SessionState) -> None:
        self._r.set(self._key(state.session_id), json.dumps(state.snapshot()), ex=self._ttl)

    # create and save are identical against a shared backend: both write the current snapshot.
    def create(self, state: SessionState) -> None:
        self._persist(state)

    def save(self, state: SessionState) -> None:
        self._persist(state)

    def get(self, session_id: str) -> Optional[SessionState]:
        raw = self._r.get(self._key(session_id))
        if raw is None:
            return None  # unknown or expired -- Redis handles eviction via key TTL
        data = json.loads(raw)
        customer = self._registry.get_by_id(data.get("customer_id"))
        if customer is None:
            return None  # customer de-provisioned since the session started; treat as gone
        state = restore(data, customer.config, self._client_factory())
        self._r.expire(self._key(session_id), self._ttl)  # touch: slide the TTL forward
        return state


def redis_store_from_url(
    url: str,
    registry,
    client_factory: Callable[[], object],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> RedisSessionStore:
    """Build a RedisSessionStore from a connection URL. `redis` is an optional dependency
    (install `.[redis]`); imported here so the in-memory path needs nothing extra."""
    import redis  # optional dependency, only needed for the shared backend

    return RedisSessionStore(
        redis.Redis.from_url(url), registry, client_factory, ttl_seconds=ttl_seconds
    )
