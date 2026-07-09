"""ProductConfig.validate() is the guardrail that makes a per-customer config safe to
accept from a form or a scraper: an internally inconsistent config must fail loudly at
load, never silently mis-route a live cancel flow."""

import pytest

from engine import Intervention, Policy, ProductConfig, ReasonDef


def base(**overrides):
    """A minimal internally-consistent config; override one field to break it."""
    kwargs = dict(
        product_name="X", product_context="", activation_definition="", pricing_summary="",
    )
    kwargs.update(overrides)
    return ProductConfig(**kwargs)


def test_default_config_is_valid():
    # validate() returns self so it can wrap a construction: ProductConfig(...).validate()
    c = base()
    assert c.validate() is c


def test_acme_fixture_validates():
    from eval.configs import ACME
    assert ACME.validate() is ACME


def test_duplicate_reason_id_rejected():
    with pytest.raises(ValueError, match="duplicate reason id"):
        base(
            reasons=[ReasonDef("a", "x"), ReasonDef("a", "y")],
            policy=Policy(preferred={"a": []}),
        ).validate()


def test_duplicate_intervention_id_rejected():
    with pytest.raises(ValueError, match="duplicate intervention id"):
        base(interventions=[
            Intervention("dup", "discount", "a"),
            Intervention("dup", "pause", "b"),
        ]).validate()


@pytest.mark.parametrize("floor", [-0.1, 1.1, 2.0])
def test_confidence_floor_out_of_range_rejected(floor):
    with pytest.raises(ValueError, match="confidence_floor"):
        base(policy=Policy(confidence_floor=floor)).validate()


def test_preferred_references_unknown_reason_rejected():
    # a single known reason, but the default preferred still references the 8 SaaS ids
    with pytest.raises(ValueError, match="unknown reason"):
        base(reasons=[ReasonDef("only_one", "desc")]).validate()


def test_reason_without_preferred_entry_rejected():
    with pytest.raises(ValueError, match="no policy.preferred entry"):
        base(
            reasons=[ReasonDef("x", "d"), ReasonDef("y", "d")],
            policy=Policy(preferred={"x": []}),  # 'y' has no entry
        ).validate()


def test_discount_reason_referencing_unknown_reason_rejected():
    with pytest.raises(ValueError, match="unknown reason"):
        base(policy=Policy(discount_reasons={"nope"})).validate()


def test_custom_but_consistent_config_validates():
    """A non-SaaS taxonomy is fine as long as reasons/preferred/policy line up."""
    cfg = base(
        reasons=[ReasonDef("habit_broke", "stopped the routine"),
                 ReasonDef("too_clinical", "wanted warmth, got a spreadsheet")],
        interventions=[Intervention("gift_month", "gift", "a free month")],
        policy=Policy(
            preferred={"habit_broke": ["gift"], "too_clinical": []},
            discount_reasons=set(),
            let_go_reasons=set(),
        ),
    )
    assert cfg.validate() is cfg
