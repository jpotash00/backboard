"""The interviewer's control flow, driven by a fake LLM client so it's deterministic
and API-free. We test the loop mechanics (ask vs diagnose, the MAX_TURNS force,
robust JSON parsing) -- not the model's judgement, which is what the eval measures."""

import json
from dataclasses import dataclass

from engine import Interviewer, ProductConfig, UserContext
from engine.interviewer import MAX_TURNS


@dataclass
class _Block:
    text: str


@dataclass
class _Resp:
    content: list


class FakeClient:
    """Returns a scripted sequence of model outputs, one per .create() call.
    Emulates the anthropic surface: client.messages.create(...)."""
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls += 1
                return _Resp(content=[_Block(text=outer._outputs.pop(0))])

        return _M()


def _config():
    return ProductConfig(
        product_name="Acme", product_context="ctx", activation_definition="act",
        pricing_summary="$49", interventions=[],
    )


def _user():
    return UserContext(user_id="u", plan="Starter", mrr=49, tenure_days=30,
                       logins_last_30d=2, activated=False)


def test_diagnose_on_first_turn_returns_outcome():
    client = FakeClient([
        json.dumps({"action": "ask", "message": "What's up?"}),          # open()
        json.dumps({"action": "diagnose", "reason": "never_activated",
                    "confidence": 0.82, "evidence": "never connected",
                    "cover_story": "too_expensive", "savable": True,
                    "message": "Thanks."}),
    ])
    iv = Interviewer(_config(), _user(), client=client)
    assert iv.open() == "What's up?"
    q, outcome = iv.turn("it's too pricey")
    assert q is None
    assert outcome.reason == "never_activated"
    assert outcome.confidence == 0.82
    assert outcome.intervention_id is None  # policy fills this, never the model
    assert outcome.turns_used == 1


def test_ask_then_diagnose():
    client = FakeClient([
        json.dumps({"action": "ask", "message": "Q0?"}),   # open()
        json.dumps({"action": "ask", "message": "Q1?"}),   # turn 1 -> another question
        json.dumps({"action": "diagnose", "reason": "price_value_mismatch",
                    "confidence": 0.7, "evidence": "e", "cover_story": "too_expensive",
                    "savable": True, "message": "ok"}),      # turn 2 -> diagnose
    ])
    iv = Interviewer(_config(), _user(), client=client)
    iv.open()
    q1, o1 = iv.turn("a")
    assert q1 == "Q1?" and o1 is None
    q2, o2 = iv.turn("b")
    assert q2 is None and o2.reason == "price_value_mismatch"
    assert o2.turns_used == 2


def test_max_turns_forces_a_diagnosis_even_if_model_asks():
    # Model keeps trying to ask; the harness must force a diagnosis at MAX_TURNS.
    asks = [json.dumps({"action": "ask", "message": f"Q{i}?"}) for i in range(MAX_TURNS + 2)]
    iv = Interviewer(_config(), _user(), client=FakeClient(asks))
    iv.open()
    outcome = None
    for i in range(MAX_TURNS):
        q, outcome = iv.turn("stalling")
    # On the final allowed turn, even an "ask" is coerced into an Outcome.
    assert outcome is not None
    assert outcome.turns_used == MAX_TURNS


def test_unparseable_output_degrades_to_unknown():
    client = FakeClient([
        "not json at all",   # open()
        "still not json",    # turn 1
    ])
    iv = Interviewer(_config(), _user(), client=client)
    iv.open()  # falls back to default question, no crash
    q, outcome = iv.turn("hi")
    assert outcome.reason == "unknown"
    assert outcome.confidence == 0.0


def test_code_fenced_json_is_stripped():
    client = FakeClient([
        "```json\n" + json.dumps({"action": "ask", "message": "hi?"}) + "\n```",
    ])
    iv = Interviewer(_config(), _user(), client=client)
    assert iv.open() == "hi?"
