"""POST /configs -- runtime customer provisioning. The headline test proves the whole point:
a customer created through the endpoint is usable on the very next request, with no restart.
The rest guard the admin auth boundary (this must never sit behind a publishable key)."""

import json

from dataclasses import dataclass

from fastapi.testclient import TestClient

from api.app import create_app
from api.registry import CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger

ADMIN = "sk_admin_secret"


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
                return _Resp(content=[_Block(text=outer._outputs.pop(0))])

        return _M()


def _app(tmp_path, outputs=None, admin_key=ADMIN):
    """A fresh app with an EMPTY registry and a real (tmp) config dir, so provisioning writes a
    file and hot-registers into the live registry."""
    return TestClient(create_app(
        registry=CustomerRegistry(),
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: ScriptedClient(outputs or []),
        admin_key=admin_key,
        config_dir=str(tmp_path / "configs"),
    ))


def _spec(customer_id="acme", public_key="pk_live_acme"):
    return {
        "customer_id": customer_id,
        "public_key": public_key,
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


def _admin(key=ADMIN):
    return {"Authorization": f"Bearer {key}"}


def test_provisioned_customer_is_live_with_no_restart(tmp_path):
    import time
    from api.identity import sign_identity

    # A scripted interview so the immediately-following /sessions call can run against the new key.
    client = _app(tmp_path, outputs=[
        json.dumps({"action": "ask", "message": "What's prompting the cancellation?"}),
    ])

    r = client.post("/configs", json=_spec(), headers=_admin())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "registered"
    assert body["public_key"] == "pk_live_acme"
    assert len(body["signing_secret"]) == 64          # minted server-side, returned once
    assert body["offers"] == 2

    # THE POINT: open a session with the brand-new key against the SAME running app -- no restart.
    # The provisioned customer requires signed identity (secure by default), and the secret we
    # just got back mints a valid token, so this proves hot-register AND the returned secret.
    token = sign_identity(body["signing_secret"],
                          {"user_id": "u1", "plan": "Growth", "mrr": 199}, int(time.time()))
    s = client.post("/sessions", json={"user_id": "u1", "identity_token": token},
                    headers={"Authorization": "Bearer pk_live_acme"})
    assert s.status_code == 200
    assert s.json()["message"] == "What's prompting the cancellation?"

    # And it was persisted, so a restart would reload it too.
    assert (tmp_path / "configs" / "acme.json").exists()


def test_missing_admin_key_config_disables_the_endpoint(tmp_path):
    client = _app(tmp_path, admin_key=None)          # no OFFBOARD_ADMIN_KEY -> feature off
    r = client.post("/configs", json=_spec(), headers=_admin())
    assert r.status_code == 404                        # not even discoverable


def test_wrong_admin_key_is_401(tmp_path):
    client = _app(tmp_path)
    assert client.post("/configs", json=_spec(), headers=_admin("nope")).status_code == 401
    assert client.post("/configs", json=_spec()).status_code == 401  # no header


def test_a_publishable_key_cannot_provision(tmp_path):
    # The admin surface must not accept a public pk_ key -- that would let anyone mint tenants.
    client = _app(tmp_path)
    r = client.post("/configs", json=_spec(), headers={"Authorization": "Bearer pk_demo_acme"})
    assert r.status_code == 401


def test_duplicate_customer_is_409(tmp_path):
    client = _app(tmp_path, outputs=[])
    assert client.post("/configs", json=_spec(), headers=_admin()).status_code == 201
    dup = client.post("/configs", json=_spec(public_key="pk_live_other"), headers=_admin())
    assert dup.status_code == 409                      # same customer_id

    dupkey = client.post("/configs", json=_spec(customer_id="other"), headers=_admin())
    assert dupkey.status_code == 409                   # same public_key


def test_empty_offer_menu_is_422(tmp_path):
    client = _app(tmp_path)
    spec = _spec()
    spec["offers"] = []
    assert client.post("/configs", json=spec, headers=_admin()).status_code == 422


def test_missing_product_fact_is_422(tmp_path):
    client = _app(tmp_path)
    spec = _spec()
    del spec["product"]["activation_definition"]
    assert client.post("/configs", json=spec, headers=_admin()).status_code == 422


# --- GET /configs (admin roster for the console) ---

def test_list_configs_requires_admin(tmp_path):
    client = _app(tmp_path)
    assert client.get("/configs").status_code == 401                       # no header
    assert client.get("/configs", headers=_admin("nope")).status_code == 401
    # A publishable key must not read the roster either.
    assert client.get("/configs", headers={"Authorization": "Bearer pk_demo_acme"}).status_code == 401


def test_list_configs_summarizes_without_leaking_secret(tmp_path):
    client = _app(tmp_path)
    client.post("/configs", json=_spec(), headers=_admin())
    rows = client.get("/configs", headers=_admin()).json()["customers"]
    assert len(rows) == 1
    row = rows[0]
    assert row["customer_id"] == "acme"
    assert row["public_key"] == "pk_live_acme"
    assert row["offers"] == 2
    assert row["has_signing_secret"] is True               # provisioned tenants are signed-mode
    assert "signing_secret" not in row                     # never exposed by the read surface


def test_list_configs_disabled_without_admin_key(tmp_path):
    client = _app(tmp_path, admin_key=None)
    assert client.get("/configs", headers=_admin()).status_code == 404


def test_admin_console_page_served_only_when_enabled(tmp_path):
    on = _app(tmp_path)
    page = on.get("/admin")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    off = _app(tmp_path, admin_key=None)
    assert off.get("/admin").status_code == 404
