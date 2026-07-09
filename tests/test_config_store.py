"""The file-backed config store + its registry integration — the production-shaped path
where customer configs are stored records rather than in-code Python. Contract: load a
directory of customer files, validate each on load, and fail loudly on anything broken."""

import json

import pytest

from api.config_store import load_customer_file, registry_from_dir
from api.registry import DEMO_PUBLIC_KEY, default_registry
from engine import config_to_dict
from eval.configs import ACME


def write_customer(directory, customer_id, public_key, config=ACME):
    path = directory / f"{customer_id}.json"
    path.write_text(json.dumps({
        "customer_id": customer_id,
        "public_key": public_key,
        "config": config_to_dict(config),
    }))
    return path


def test_load_customer_file(tmp_path):
    path = write_customer(tmp_path, "acme", "pk_acme")
    customer = load_customer_file(path)
    assert customer.id == "acme"
    assert customer.public_key == "pk_acme"
    assert customer.config.product_name == ACME.product_name


def test_registry_from_dir_loads_all(tmp_path):
    write_customer(tmp_path, "acme", "pk_acme")
    write_customer(tmp_path, "beta", "pk_beta")
    reg = registry_from_dir(tmp_path)
    assert reg.get_by_key("pk_acme").id == "acme"
    assert reg.get_by_key("pk_beta").id == "beta"
    assert reg.get_by_key("pk_missing") is None


def test_empty_dir_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        registry_from_dir(tmp_path)


def test_broken_json_fails_at_load(tmp_path):
    (tmp_path / "bad.json").write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        registry_from_dir(tmp_path)


def test_invalid_config_fails_at_load_not_mid_interview(tmp_path):
    # a structurally valid file whose config is internally inconsistent
    (tmp_path / "bad.json").write_text(json.dumps({
        "customer_id": "x", "public_key": "pk_x",
        "config": {
            "product_name": "X", "product_context": "", "activation_definition": "",
            "pricing_summary": "",
            "reasons": [{"id": "a", "description": "d"}],
            "policy": {"preferred": {"ghost": []}},
        },
    }))
    with pytest.raises(ValueError):
        registry_from_dir(tmp_path)


# --- registry env integration ---

def test_default_registry_seeds_demo_without_env(monkeypatch):
    monkeypatch.delenv("OFFBOARD_CONFIG_DIR", raising=False)
    reg = default_registry()
    assert reg.get_by_key(DEMO_PUBLIC_KEY).id == "acme"


def test_default_registry_loads_from_config_dir_when_set(tmp_path, monkeypatch):
    write_customer(tmp_path, "acme", "pk_from_disk")
    monkeypatch.setenv("OFFBOARD_CONFIG_DIR", str(tmp_path))
    reg = default_registry()
    assert reg.get_by_key("pk_from_disk").id == "acme"
    # the in-code demo key is NOT present when loading from disk
    assert reg.get_by_key(DEMO_PUBLIC_KEY) is None
