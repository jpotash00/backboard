"""The config proposer: LLM structures messy input into a *validated* ProductConfig.
Driven by a scripted fake client -- no ANTHROPIC_API_KEY needed. The safety property is
that a proposal is always either a config that passes validate() or a clear ProposalError."""

import json
from dataclasses import dataclass

import pytest

from engine import decide, Outcome, UserContext
from onboarding import ProposalError, ProposalInput, propose_config
from onboarding.adapters import PasteAdapter, UrlScrapeAdapter


@dataclass
class _Block:
    text: str


@dataclass
class _Resp:
    content: list


class FakeClient:
    def __init__(self, output: str):
        self._output = output

    @property
    def messages(self):
        out = self._output

        class _M:
            def create(self, **_kw):
                return _Resp(content=[_Block(text=out)])
        return _M()


GOOD = json.dumps({
    "product_name": "Boxly",
    "product_context": "A snack-box subscription for offices.",
    "activation_definition": "received and rated their first box",
    "pricing_summary": "$30/box monthly, $25/box on annual",
    "competitors": ["SnackNation"],
    "known_churn_reasons": ["too many boxes piling up"],
    "interventions": [
        {"id": "discount_annual", "type": "discount", "description": "20% off annual",
         "eligible_when": "reason == price_value_mismatch AND tenure > 60"},
        {"id": "pause_box", "type": "pause", "description": "Pause deliveries for a month",
         "eligible_when": None},
        {"type": "onboarding", "description": "A quick taste-preference call"},  # no id
    ],
    "notes": "Assumed monthly billing; verify the annual price.",
})


def test_proposes_a_valid_config():
    p = propose_config(
        ProposalInput(offers=["20% off annual", "pause a month", "taste-preference call"],
                      product_text="Boxly: snack boxes for offices. $30/box."),
        client=FakeClient(GOOD),
    )
    assert p.config.product_name == "Boxly"
    # offers became typed interventions; the config is valid (validate() ran)
    types = {i.type for i in p.config.interventions}
    assert {"discount", "pause", "onboarding"} <= types
    # the taxonomy + policy came from validated defaults, not the model
    assert p.config.reason_ids() == [r.id for r in __import__("engine").DEFAULT_REASONS]
    assert p.notes and "annual price" in p.notes


def test_proposed_config_actually_drives_a_decision():
    # The whole point: the proposal is immediately usable by the engine.
    p = propose_config(ProposalInput(offers=["20% off annual"], product_text="x"),
                       client=FakeClient(GOOD))
    user = UserContext("u", "annual", 30, 120, 20, activated=True)
    out = decide(Outcome("price_value_mismatch", 0.9, "", "too_expensive", True, None, "", 1),
                 p.config, user)
    assert out.intervention_id == "discount_annual"   # the discount the model proposed


def test_missing_id_is_generated_and_ids_are_unique():
    p = propose_config(ProposalInput(offers=["a", "b", "c"], product_text="x"),
                       client=FakeClient(GOOD))
    ids = [i.id for i in p.config.interventions]
    assert len(ids) == len(set(ids))                  # no collisions
    assert all(ids)                                    # none empty (the id-less one got one)


def test_offers_are_required():
    with pytest.raises(ProposalError, match="offer"):
        propose_config(ProposalInput(offers=[], product_text="x"), client=FakeClient(GOOD))


def test_non_json_output_raises_clearly():
    with pytest.raises(ProposalError, match="valid JSON"):
        propose_config(ProposalInput(offers=["x"]), client=FakeClient("sorry, I can't"))


def test_code_fenced_json_is_handled():
    p = propose_config(ProposalInput(offers=["20% off annual"]),
                       client=FakeClient("```json\n" + GOOD + "\n```"))
    assert p.config.product_name == "Boxly"


def test_empty_interventions_raises():
    bad = json.dumps({"product_name": "X", "interventions": []})
    with pytest.raises(ProposalError, match="no usable interventions"):
        propose_config(ProposalInput(offers=["x"]), client=FakeClient(bad))


def test_paste_adapter_and_scrape_stub():
    assert PasteAdapter("hello").gather() == "hello"
    with pytest.raises(NotImplementedError):
        UrlScrapeAdapter("https://example.com/pricing").gather()
