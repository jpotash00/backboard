"""PATCH/DELETE /configs/{id}: a tenant edits or removes ITSELF from its backend using its own
signing_secret, and the operator (admin key) can manage any tenant. The properties that matter:
an edit preserves the identity live integrations depend on (same key + secret), and one customer
can never touch another's tenant (isolation)."""

import json
import time
from dataclasses import dataclass

from fastapi.testclient import TestClient

from api.app import create_app
from api.identity import sign_identity
from api.registry import CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger

ADMIN = "sk_admin_secret"
SIGNUP = "signup_code"


@dataclass
class _Block:
    text: str


@dataclass
class _Resp:
    content: list


class ScriptedClient:
    def __init__(self, outputs):
        self._outputs = list(outputs)

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **_kw):
                return _Resp(content=[_Block(text=outer._outputs.pop(0) if outer._outputs else
                                             '{"action":"ask","message":"why?"}')])

        return _M()


def _client(tmp_path):
    return TestClient(create_app(
        registry=CustomerRegistry(),
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: ScriptedClient([]),
        admin_key=ADMIN,
        signup_key=SIGNUP,
        config_dir=str(tmp_path / "configs"),
    ))


def _spec(customer_id):
    return {
        "customer_id": customer_id,
        "product": {
            "product_name": "Acme",
            "product_context": "analytics",
            "activation_definition": "connected a source",
            "pricing_summary": "$49 / $199",
        },
        "offers": [{"type": "discount", "description": "50% off for 3 months"}],
    }


def _update(offers, product_name="Acme"):
    return {
        "product": {
            "product_name": product_name,
            "product_context": "analytics",
            "activation_definition": "connected a source",
            "pricing_summary": "$49 / $199",
        },
        "offers": offers,
        "allowed_origins": [],
        "competitors": [],
    }


def _provision(client, customer_id):
    """Self-serve provision, returning the created record (public_key + signing_secret)."""
    return client.post("/onboard", json=_spec(customer_id),
                       headers={"Authorization": f"Bearer {SIGNUP}"}).json()


def _owner(secret):
    return {"Authorization": f"Bearer {secret}"}


# --- Update -------------------------------------------------------------------------------

def test_owner_updates_own_config_with_signing_secret(tmp_path):
    c = _client(tmp_path)
    rec = _provision(c, "acme")
    r = c.patch("/configs/acme",
                json=_update([{"type": "pause", "description": "Pause up to 3 months"},
                              {"type": "discount", "description": "50% off"}]),
                headers=_owner(rec["signing_secret"]))
    assert r.status_code == 200
    assert r.json()["offers"] == 2


def test_update_preserves_public_key_and_signing_secret(tmp_path):
    """The identity live integrations depend on must survive an edit: same key, same secret --
    proven by a session still opening with a token signed by the ORIGINAL secret after the edit."""
    c = _client(tmp_path)
    rec = _provision(c, "acme")
    key, secret = rec["public_key"], rec["signing_secret"]
    c.patch("/configs/acme", json=_update([{"type": "gift", "description": "Free month"}]),
            headers=_owner(secret))
    token = sign_identity(secret, {"user_id": "u1", "mrr": 49}, iat=int(time.time()))
    r = c.post("/sessions", json={"user_id": "u1", "identity_token": token},
               headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200  # original key + secret still valid after the edit


def test_admin_can_update_any_tenant(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    r = c.patch("/configs/acme", json=_update([{"type": "support", "description": "Priority support"}]),
                headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200


def test_one_tenant_cannot_edit_another(tmp_path):
    """Isolation: acme's secret must not authorize an edit of globex."""
    c = _client(tmp_path)
    _provision(c, "acme")
    globex = _provision(c, "globex")
    r = c.patch("/configs/globex", json=_update([{"type": "pause", "description": "x"}]),
                headers=_owner("acme_is_not_globex_secret"))
    assert r.status_code == 401
    # and acme's REAL secret also can't touch globex
    acme = _provision(c, "acme2")
    r2 = c.patch("/configs/globex", json=_update([{"type": "pause", "description": "x"}]),
                 headers=_owner(acme["signing_secret"]))
    assert r2.status_code == 401


def test_update_unknown_customer_by_admin_is_404(tmp_path):
    c = _client(tmp_path)
    r = c.patch("/configs/ghost", json=_update([{"type": "pause", "description": "x"}]),
                headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 404


# --- Delete -------------------------------------------------------------------------------

def test_owner_deletes_own_tenant_and_key_stops_resolving(tmp_path):
    c = _client(tmp_path)
    rec = _provision(c, "acme")
    r = c.delete("/configs/acme", headers=_owner(rec["signing_secret"]))
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    # key no longer resolves -> a session attempt 401s
    assert c.post("/sessions", json={"user_id": "u1"},
                  headers={"Authorization": f"Bearer {rec['public_key']}"}).status_code == 401


def test_admin_can_delete_any_tenant(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    assert c.delete("/configs/acme", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200


def test_cannot_delete_another_tenant(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    globex = _provision(c, "globex")
    r = c.delete("/configs/acme", headers=_owner(globex["signing_secret"]))
    assert r.status_code == 401
    # acme is still alive (was not deleted by the failed attempt)
    assert c.delete("/configs/acme", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200


def test_missing_credential_is_401(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    assert c.delete("/configs/acme").status_code == 401
    assert c.patch("/configs/acme", json=_update([{"type": "pause", "description": "x"}])).status_code == 401
