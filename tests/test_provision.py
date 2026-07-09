"""The no-UI provisioning path: a tiny spec becomes a complete, secure, loadable customer.

These prove the two safety properties hand-written JSON doesn't give you -- an always-minted
signing_secret and validate-before-write -- and that the resulting file round-trips through the
real file-drop loader into a config that drives a decision."""

import json

import pytest

from engine import Outcome, UserContext, decide
from api.config_store import load_customer_file
from onboarding.provision import (
    ProvisionError,
    build_record,
    write_customer_file,
)


def _spec():
    return {
        "customer_id": "acme",
        "public_key": "pk_live_acme",
        "allowed_origins": ["https://app.acme.com"],
        "product": {
            "product_name": "Acme Analytics",
            "product_context": "product analytics for SaaS teams",
            "activation_definition": "connected a source and built a dashboard",
            "pricing_summary": "Starter $49, Growth $199",
        },
        "offers": [
            {"type": "discount", "description": "50% off for 3 months"},
            {"type": "pause", "description": "Pause billing for up to 3 months"},
        ],
    }


def test_public_key_is_auto_generated_when_omitted():
    spec = _spec()
    del spec["public_key"]
    record = build_record(spec)
    assert record["public_key"].startswith("pk_live_")
    assert len(record["public_key"]) > len("pk_live_")  # has a random suffix


def test_auto_generated_keys_are_unique():
    a, b = _spec(), _spec()
    del a["public_key"]
    del b["public_key"]
    assert build_record(a)["public_key"] != build_record(b)["public_key"]


def test_supplied_public_key_is_respected():
    record = build_record(_spec())  # _spec provides pk_live_acme
    assert record["public_key"] == "pk_live_acme"


def test_blank_public_key_is_treated_as_omitted():
    spec = _spec()
    spec["public_key"] = ""
    assert build_record(spec)["public_key"].startswith("pk_live_")


def test_minimal_spec_produces_a_complete_secure_envelope():
    record = build_record(_spec())
    # A secret is ALWAYS minted -- you can't accidentally ship a trust-the-browser tenant.
    assert len(record["signing_secret"]) == 64
    assert record["public_key"] == "pk_live_acme"
    assert record["allowed_origins"] == ["https://app.acme.com"]
    cfg = record["config"]
    # Only product facts + offers were supplied; taxonomy + policy come from defaults.
    assert cfg["product_name"] == "Acme Analytics"
    assert len(cfg["reasons"]) == 8                     # the default SaaS taxonomy
    assert cfg["policy"]["rank_by"] == "preferred"      # default policy present
    assert len(cfg["interventions"]) == 2


def test_provided_secret_is_reused_not_regenerated():
    record = build_record(_spec(), signing_secret="deadbeef" * 8)
    assert record["signing_secret"] == "deadbeef" * 8


def test_missing_product_field_fails_at_provision_time():
    spec = _spec()
    del spec["product"]["activation_definition"]
    with pytest.raises(ProvisionError, match="activation_definition"):
        build_record(spec)


def test_empty_offer_menu_is_rejected():
    spec = _spec()
    spec["offers"] = []
    with pytest.raises(ProvisionError, match="offer menu"):
        build_record(spec)


def test_missing_customer_id_is_rejected():
    # customer_id is the one required identity field; public_key auto-generates (tested above).
    spec = _spec()
    del spec["customer_id"]
    with pytest.raises(ProvisionError, match="customer_id"):
        build_record(spec)


def test_written_file_round_trips_through_the_loader_and_drives_a_decision(tmp_path):
    record = build_record(_spec())
    path = write_customer_file(record, str(tmp_path))
    assert path.name == "acme.json"

    # The real file-drop loader must accept it, secret and origins intact.
    customer = load_customer_file(path)
    assert customer.id == "acme"
    assert customer.public_key == "pk_live_acme"
    assert customer.signing_secret == record["signing_secret"]
    assert customer.allowed_origins == ("https://app.acme.com",)

    # And the defaulted config actually authorizes: a price churner gets an offer from the menu.
    outcome = Outcome(
        reason="price_value_mismatch", confidence=0.9, evidence="daily user, cost",
        cover_story="too_expensive", savable=True, intervention_id=None,
        rationale="", turns_used=1,
    )
    user = UserContext(user_id="u", plan="Growth", mrr=199, tenure_days=200,
                       logins_last_30d=20, activated=True)
    decided = decide(outcome, customer.config, user)
    assert decided.intervention_id is not None          # the offer menu is live end-to-end


def test_write_refuses_to_clobber_and_rotate_the_secret(tmp_path):
    record = build_record(_spec())
    write_customer_file(record, str(tmp_path))
    # A second write for the same customer must NOT silently rotate the secret.
    with pytest.raises(ProvisionError, match="already exists"):
        write_customer_file(build_record(_spec()), str(tmp_path))


def test_example_spec_file_is_valid(tmp_path):
    # The documented template must actually provision (docs that don't run are a lie).
    import pathlib
    spec = json.loads(pathlib.Path("docs/customer-spec.example.json").read_text())
    record = build_record(spec)
    assert record["config"]["interventions"]            # offers came through
    assert len(record["signing_secret"]) == 64
