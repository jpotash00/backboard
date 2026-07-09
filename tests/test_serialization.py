"""ProductConfig (de)serialization: round-trip fidelity, partial-config defaults,
and validate-on-load. This is the contract the file/DB config store depends on."""

import json

import pytest

from engine import (
    Policy,
    ProductConfig,
    config_from_dict,
    config_from_json,
    config_to_dict,
    config_to_json,
)
from eval.configs import ACME


def test_round_trip_preserves_everything():
    restored = config_from_dict(config_to_dict(ACME))
    # Full-fidelity round trip, including the policy sets that JSON can't hold natively.
    assert restored.product_name == ACME.product_name
    assert restored.reason_ids() == ACME.reason_ids()
    assert [i.id for i in restored.interventions] == [i.id for i in ACME.interventions]
    assert restored.policy.preferred == ACME.policy.preferred
    assert restored.policy.discount_reasons == ACME.policy.discount_reasons
    assert restored.policy.let_go_reasons == ACME.policy.let_go_reasons
    assert restored.policy.confidence_floor == ACME.policy.confidence_floor


def test_json_string_round_trip_is_stable():
    once = config_to_json(ACME)
    twice = config_to_json(config_from_json(once))
    assert once == twice
    assert json.loads(once)["policy"]["discount_reasons"] == ["price_value_mismatch"]


def test_partial_config_inherits_defaults():
    # A customer who submits only product fields inherits the default SaaS taxonomy
    # and rulebook -- the common onboarding case.
    c = config_from_dict({
        "product_name": "Zen",
        "product_context": "A meditation app.",
        "activation_definition": "completed one session",
        "pricing_summary": "$9/mo",
    })
    assert c.reason_ids() == ProductConfig("", "", "", "").reason_ids()
    assert c.policy.confidence_floor == Policy().confidence_floor


def test_custom_taxonomy_and_policy_round_trip():
    c = ProductConfig(
        product_name="Boxly", product_context="Snack subscription boxes.",
        activation_definition="received first box", pricing_summary="$30/box",
        reasons=[__import__("engine").ReasonDef("too_many_boxes", "piling up unused"),
                 __import__("engine").ReasonDef("unknown", "cannot resolve")],
        policy=Policy(preferred={"too_many_boxes": ["pause"], "unknown": []},
                      discount_reasons=set(), let_go_reasons={"too_many_boxes"}),
    )
    restored = config_from_dict(config_to_dict(c))
    assert restored.reason_ids() == ["too_many_boxes", "unknown"]
    assert restored.policy.let_go_reasons == {"too_many_boxes"}


def test_bad_config_rejected_on_load():
    with pytest.raises(ValueError):
        config_from_dict({
            "product_name": "X", "product_context": "", "activation_definition": "",
            "pricing_summary": "", "reasons": [{"id": "a", "description": "x"}],
            "policy": {"preferred": {"nonexistent": []}},
        })
