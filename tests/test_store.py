"""Store seam tests: TTL eviction (the in-memory leak guard) and the snapshot/restore
round-trip that a Redis/DB backend rides on for horizontal scale."""

from engine import Interviewer, Outcome, UserContext
from eval.configs import ACME
from api.store import SessionState, SessionStore, restore


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
