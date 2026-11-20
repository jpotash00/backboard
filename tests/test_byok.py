"""Bring-your-own-Claude: each tenant's interviews bill to the tenant's own Anthropic key. The key
is write-only (stored encrypted, never returned), survives config edits, and a tenant without one
gets a clean 409 instead of silently spending the operator's key."""

import json

from fastapi.testclient import TestClient

from api.app import _lazy_anthropic_factory, create_app
from api.crypto import generate_key
from api.registry import CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger
from tests.test_admin_configs import ADMIN, _spec

KEY = "sk-ant-tenant-key-123"


def _client(tmp_path):
    return TestClient(create_app(
        registry=CustomerRegistry(),
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=_lazy_anthropic_factory(),  # the real per-tenant factory
        admin_key=ADMIN,
        config_dir=str(tmp_path / "configs"),
    ))


def test_tenant_key_is_encrypted_write_only_and_kept_on_update(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", generate_key())
    c = _client(tmp_path)
    auth = {"Authorization": f"Bearer {ADMIN}"}

    r = c.post("/configs", json={**_spec(), "anthropic_api_key": KEY}, headers=auth)
    assert r.status_code == 201 and KEY not in r.text

    on_disk = (tmp_path / "configs" / "acme.json").read_text()
    assert KEY not in on_disk and json.loads(on_disk)["anthropic_api_key"].startswith("enc:")

    listing = c.get("/configs", headers=auth)
    assert KEY not in listing.text and listing.json()["customers"][0]["has_anthropic_key"]

    # An edit that omits the key keeps it; one that sends a new key replaces it.
    edit = {k: v for k, v in _spec().items() if k not in ("customer_id", "public_key")}
    assert c.patch("/configs/acme", json=edit, headers=auth).status_code == 200
    assert c.get("/configs", headers=auth).json()["customers"][0]["has_anthropic_key"]


def test_tenant_without_key_gets_409_not_operator_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator")  # must NOT be used for tenants
    c = _client(tmp_path)
    spec = _spec(customer_id="nokey", public_key="pk_live_nokey")
    assert c.post("/configs", json=spec, headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 201

    r = c.post("/sessions", json={"user_id": "u1"},
               headers={"Authorization": "Bearer pk_live_nokey"})
    assert r.status_code == 409 and "Claude API key" in r.json()["detail"]
