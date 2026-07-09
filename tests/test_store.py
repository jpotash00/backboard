"""Store seam tests: TTL eviction (the in-memory leak guard), the snapshot/restore
round-trip that a Redis/DB backend rides on for horizontal scale, and the RedisSessionStore
itself driven against a fake redis client (no server needed)."""

from engine import Interviewer, Outcome, UserContext
from eval.configs import ACME
from api.registry import Customer, CustomerRegistry
from api.store import (
    RedisSessionStore,
    SessionState,
    SessionStore,
    restore,
)


class _FakeModel:
    """Never called during snapshot/restore; only needed to construct an Interviewer."""

    messages = None


def _user():
    return UserContext(
        user_id="u1", plan="Growth", mrr=199, tenure_days=200,
        logins_last_30d=12, activated=True, usage_summary="daily", signals={"seats_used": 7},
    )


def _state():
    user = _user()
    interviewer = Interviewer(ACME, user, client=_FakeModel())
    interviewer.messages = [
        {"role": "user", "content": "<cancel flow opened>"},
        {"role": "assistant", "content": '{"action": "ask", "message": "why?"}'},
    ]
    interviewer.turns = 1
    return SessionState(
        session_id="sess1",
        customer_id="acme",
        interviewer=interviewer,
        config=ACME,
        user=user,
        transcript=[{"speaker": "interviewer", "text": "why?"}],
        outcome=Outcome(
            reason="price_value_mismatch", confidence=0.8, evidence="caps",
            cover_story="too_expensive", savable=True, intervention_id="discount_50_3mo",
            rationale="r", turns_used=1,
        ),
        done=True,
        resolution={"accepted": True},
    )


def test_snapshot_restore_round_trip():
    original = _state()
    data = restore(original.snapshot(), ACME, client=_FakeModel())

    assert data.session_id == original.session_id
    assert data.customer_id == original.customer_id
    assert data.user == original.user
    assert data.transcript == original.transcript
    assert data.done is True
    assert data.resolution == {"accepted": True}
    # The interview context survives the boundary: message history + turn count intact.
    assert data.interviewer.messages == original.interviewer.messages
    assert data.interviewer.turns == 1
    # The declared outcome round-trips field for field.
    assert data.outcome.intervention_id == "discount_50_3mo"
    assert data.outcome.reason == "price_value_mismatch"


def test_ttl_evicts_abandoned_sessions():
    clock = {"t": 1000.0}
    store = SessionStore(now=lambda: clock["t"], ttl_seconds=60)
    store.create(_state())
    assert store.get("sess1") is not None      # fresh
    clock["t"] += 30
    assert store.get("sess1") is not None      # touched, still alive
    clock["t"] += 61
    assert store.get("sess1") is None          # abandoned past TTL -> evicted


def test_active_session_is_kept_alive_by_touch():
    clock = {"t": 0.0}
    store = SessionStore(now=lambda: clock["t"], ttl_seconds=60)
    store.create(_state())
    for _ in range(10):
        clock["t"] += 50
        assert store.get("sess1") is not None  # each access refreshes last-seen


# --------------------------------------------------------------------------------------
# RedisSessionStore -- driven against a minimal fake redis (get/set(ex)/expire/delete only).
# --------------------------------------------------------------------------------------
class _FakeRedis:
    """Just enough of the redis-py surface the store uses. `set(ex=...)` records the TTL;
    `expire` updates it; both are exposed so tests can assert the sliding-TTL behavior."""

    def __init__(self):
        self.kv: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    def set(self, key, value, ex=None):
        self.kv[key] = value if isinstance(value, bytes) else value.encode()
        if ex is not None:
            self.ttl[key] = ex

    def get(self, key):
        return self.kv.get(key)

    def expire(self, key, seconds):
        if key in self.kv:
            self.ttl[key] = seconds

    def delete(self, key):
        self.kv.pop(key, None)
        self.ttl.pop(key, None)


def _registry():
    reg = CustomerRegistry()
    reg.register(Customer(id="acme", public_key="pk_demo_acme", config=ACME))
    return reg


def _redis_store(fake=None):
    fake = fake or _FakeRedis()
    store = RedisSessionStore(
        fake, _registry(), client_factory=lambda: _FakeModel(), ttl_seconds=120,
    )
    return store, fake


def test_redis_store_persists_and_rehydrates_across_processes():
    store, fake = _redis_store()
    store.create(_state())

    # A *different* store instance (simulating another app instance) reading the same Redis
    # must recover the full session -- the whole point of the shared backend.
    other, _ = _redis_store(fake)
    got = other.get("sess1")
    assert got is not None
    assert got.customer_id == "acme"
    assert got.transcript == [{"speaker": "interviewer", "text": "why?"}]
    assert got.done is True
    assert got.resolution == {"accepted": True}
    # Interview context and the declared outcome survive the boundary.
    assert got.interviewer.turns == 1
    assert got.interviewer.messages[0]["content"] == "<cancel flow opened>"
    assert got.outcome.intervention_id == "discount_50_3mo"
    # Config was re-attached from the registry by customer_id, not serialized.
    assert got.config is ACME


def test_redis_store_writes_a_ttl_and_slides_it_on_read():
    store, fake = _redis_store()
    store.create(_state())
    assert fake.ttl["offboard:session:sess1"] == 120   # set on write
    fake.ttl["offboard:session:sess1"] = 5             # simulate time passing
    store.get("sess1")
    assert fake.ttl["offboard:session:sess1"] == 120   # refreshed on read


def test_redis_store_missing_key_is_none():
    store, _ = _redis_store()
    assert store.get("nope") is None


def test_redis_store_deprovisioned_customer_reads_as_gone():
    # Session written, then its customer removed from the registry -> safe "not found".
    fake = _FakeRedis()
    RedisSessionStore(fake, _registry(), lambda: _FakeModel(), ttl_seconds=120).create(_state())
    empty = RedisSessionStore(fake, CustomerRegistry(), lambda: _FakeModel())
    assert empty.get("sess1") is None
