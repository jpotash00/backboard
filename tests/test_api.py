"""End-to-end API tests over the session endpoints, driven by a scripted fake model
client so they're deterministic and need no ANTHROPIC_API_KEY. We exercise the real
routing, auth, session store, and policy wiring -- only the LLM is faked."""

import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.registry import DEMO_PUBLIC_KEY
from api.store import SessionStore
from api.transcripts import TranscriptLogger


@dataclass
class _Block:
    text: str


@dataclass
class _Resp:
    content: list


class ScriptedClient:
    """One shared fake model client; returns queued outputs in order across the whole
    session (open() consumes one, each turn() consumes one)."""
    def __init__(self, outputs):
        self._outputs = list(outputs)

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **_kw):
                return _Resp(content=[_Block(text=outer._outputs.pop(0))])

        return _M()


def make_client(outputs, tmp_path):
    app = create_app(
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: ScriptedClient(outputs),
    )
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {DEMO_PUBLIC_KEY}"}


def test_full_session_never_activated_gets_onboarding(tmp_path):
    client = make_client([
        # open()
        json.dumps({"action": "ask", "message": "What's prompting the cancellation?"}),
        # turn 1 -> diagnose
        json.dumps({"action": "diagnose", "reason": "never_activated",
                    "confidence": 0.9, "evidence": "never connected a source",
                    "cover_story": "too_expensive", "savable": False,
                    "message": "Totally fair — thanks for the honesty."}),
    ], tmp_path)

    r = client.post("/sessions", json={"user_id": "u1", "activated": False,
                                        "tenure_days": 61, "logins_last_30d": 1}, headers=AUTH)
    assert r.status_code == 200
    session_id = r.json()["session_id"]
    assert r.json()["message"] == "What's prompting the cancellation?"

    r2 = client.post(f"/sessions/{session_id}/turn",
                     json={"user_message": "it's too expensive"}, headers=AUTH)
    assert r2.status_code == 200
    body = r2.json()
    assert body["done"] is True
    assert body["message"] == "Totally fair — thanks for the honesty."
    out = body["outcome"]
    assert out["reason"] == "never_activated"
    assert out["cover_story"] == "too_expensive"
    # Policy — not the model — picks onboarding, and NEVER a discount here.
    assert out["intervention_id"] == "setup_call_15m"
    # The offer is resolved so the host can render it without re-fetching config.
    assert body["intervention"]["type"] == "onboarding"
    assert "setup call" in body["intervention"]["description"].lower()
    # The decision is declared end-to-end: economics + an auditable trace over the wire.
    assert out["economics"]["customer_value"] == out["economics"]["customer_value"]
    assert any(e.get("chosen") for e in out["decision_trace"])
    # never_activated + activated:false = corroborated; at 0.9 that's confident enough to act.
    assert out["corroboration"]["status"] == "corroborated"
    assert out["mode"] == "act"

    # Transcript logged for the data asset.
    log = (tmp_path / "sessions.jsonl").read_text().strip()
    assert log
    record = json.loads(log)
    assert record["outcome"]["reason"] == "never_activated"
    # open question, churner answer, closing message (the diagnose turn adds no question)
    assert len(record["transcript"]) == 3


def test_multi_turn_then_diagnose(tmp_path):
    client = make_client([
        json.dumps({"action": "ask", "message": "Q0?"}),
        json.dumps({"action": "ask", "message": "Q1?"}),
        json.dumps({"action": "diagnose", "reason": "price_value_mismatch",
                    "confidence": 0.85, "evidence": "daily user hitting caps",
                    "cover_story": "too_expensive", "savable": True,
                    "message": "Got it."}),
    ], tmp_path)

    sid = client.post("/sessions", json={"user_id": "u2", "activated": True,
                                         "tenure_days": 200}, headers=AUTH).json()["session_id"]
    r1 = client.post(f"/sessions/{sid}/turn", json={"user_message": "too pricey"}, headers=AUTH)
    assert r1.json() == {"message": "Q1?", "done": False, "outcome": None,
                         "intervention": None}
    r2 = client.post(f"/sessions/{sid}/turn", json={"user_message": "yeah the caps"}, headers=AUTH)
    assert r2.json()["done"] is True
    assert r2.json()["outcome"]["intervention_id"] == "discount_50_3mo"


def test_resolved_session_replays_outcome_idempotently(tmp_path):
    client = make_client([
        json.dumps({"action": "ask", "message": "hi?"}),
        json.dumps({"action": "diagnose", "reason": "value_ended", "confidence": 0.8,
                    "evidence": "project wrapped", "cover_story": "not_using_it",
                    "savable": False, "message": "bye"}),
    ], tmp_path)
    sid = client.post("/sessions", json={"user_id": "u3"}, headers=AUTH).json()["session_id"]
    first = client.post(f"/sessions/{sid}/turn", json={"user_message": "done with it"}, headers=AUTH)
    again = client.post(f"/sessions/{sid}/turn", json={"user_message": "hello?"}, headers=AUTH)
    assert first.json()["outcome"] == again.json()["outcome"]
    assert again.json()["done"] is True


def test_signals_reach_the_interviewer_prompt(tmp_path):
    """Product-specific signals in the POST body must render into the model's context."""
    seen = {}

    class CapturingClient:
        @property
        def messages(self):
            class _M:
                def create(self, **kw):
                    seen["system"] = kw.get("system", "")
                    return _Resp(content=[_Block(text=json.dumps(
                        {"action": "ask", "message": "hi?"}))])
            return _M()

    app = create_app(
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: CapturingClient(),
    )
    client = TestClient(app)
    r = client.post("/sessions", json={"user_id": "u", "signals": {"seats_used": 7}},
                    headers=AUTH)
    assert r.status_code == 200
    # `system` is a list of content blocks (a cached static block + the per-user block);
    # concatenate their text before asserting the signal rendered into the prompt.
    system = seen["system"]
    system_text = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert "seats_used=7" in system_text


def test_health_needs_no_auth(tmp_path):
    client = make_client([], tmp_path)
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_auth_is_401(tmp_path):
    client = make_client(["{}"], tmp_path)
    assert client.post("/sessions", json={"user_id": "u"}).status_code == 401


def test_bad_key_is_401(tmp_path):
    client = make_client(["{}"], tmp_path)
    r = client.post("/sessions", json={"user_id": "u"},
                    headers={"Authorization": "Bearer pk_wrong"})
    assert r.status_code == 401


def test_unknown_session_is_404(tmp_path):
    client = make_client([], tmp_path)
    r = client.post("/sessions/deadbeef/turn", json={"user_message": "hi"}, headers=AUTH)
    assert r.status_code == 404


def test_empty_user_message_is_422(tmp_path):
    client = make_client(["{}"], tmp_path)
    # need a real session first
    client2 = make_client([json.dumps({"action": "ask", "message": "hi?"})], tmp_path)
    sid = client2.post("/sessions", json={"user_id": "u"}, headers=AUTH).json()["session_id"]
    r = client2.post(f"/sessions/{sid}/turn", json={"user_message": ""}, headers=AUTH)
    assert r.status_code == 422


def _resolve_a_session(tmp_path):
    """Run a session to a diagnosis (price_value_mismatch -> discount) and return its id."""
    client = make_client([
        json.dumps({"action": "ask", "message": "why?"}),
        json.dumps({"action": "diagnose", "reason": "price_value_mismatch",
                    "confidence": 0.9, "evidence": "daily user, cost", "cover_story": "too_expensive",
                    "savable": True, "message": "got it"}),
    ], tmp_path)
    sid = client.post("/sessions", json={"user_id": "u", "activated": True,
                                         "tenure_days": 200}, headers=AUTH).json()["session_id"]
    client.post(f"/sessions/{sid}/turn", json={"user_message": "too pricey"}, headers=AUTH)
    return client, sid


def test_resolution_accepted_is_logged(tmp_path):
    client, sid = _resolve_a_session(tmp_path)
    r = client.post(f"/sessions/{sid}/resolution", json={"accepted": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "recorded"

    rec = json.loads((tmp_path / "resolutions.jsonl").read_text().strip())
    assert rec["accepted"] is True
    assert rec["offered"] is True
    assert rec["intervention_id"] == "discount_50_3mo"
    assert rec["intervention_type"] == "discount"
    assert rec["reason"] == "price_value_mismatch"


def test_resolution_is_idempotent(tmp_path):
    client, sid = _resolve_a_session(tmp_path)
    client.post(f"/sessions/{sid}/resolution", json={"accepted": False}, headers=AUTH)
    client.post(f"/sessions/{sid}/resolution", json={"accepted": True}, headers=AUTH)  # retry
    lines = (tmp_path / "resolutions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1                       # recorded once
    assert json.loads(lines[0])["accepted"] is False   # the first decision stands


def test_resolution_unknown_session_is_404(tmp_path):
    client = make_client([], tmp_path)
    r = client.post("/sessions/nope/resolution", json={"accepted": True}, headers=AUTH)
    assert r.status_code == 404


def test_resolution_needs_auth(tmp_path):
    client, sid = _resolve_a_session(tmp_path)
    assert client.post(f"/sessions/{sid}/resolution", json={"accepted": True}).status_code == 401
