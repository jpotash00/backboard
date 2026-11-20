"""Self-serve onboarding (/onboard): a customer provisions ITSELF from a shared signup link,
with a create-only signup code that is NOT the master admin key. The security properties that
matter: it's disabled unless configured, the code is enforced, and a customer can never see or
manage another tenant through this surface (isolation)."""

import json
from dataclasses import dataclass

from fastapi.testclient import TestClient

from api.app import create_app
from api.registry import CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger

ADMIN = "sk_admin_secret"
SIGNUP = "signup_shared_code"


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


def _app(tmp_path, admin_key=ADMIN, outputs=None):
    return TestClient(create_app(
        registry=CustomerRegistry(),
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda *_: ScriptedClient(outputs or []),
        admin_key=admin_key,
        config_dir=str(tmp_path / "configs"),
    ))


def _spec(customer_id="acme"):
    # No public_key -- self-serve relies on server auto-generation.
    return {
        "customer_id": customer_id,
        "product": {
            "product_name": "Acme Analytics",
            "product_context": "product analytics for SaaS teams",
            "activation_definition": "connected a source and built a dashboard",
            "pricing_summary": "Starter $49, Growth $199",
        },
        "offers": [{"type": "discount", "description": "50% off for 3 months"}],
        "anthropic_api_key": "sk-ant-test",  # self-serve tenants must bring their own Claude key
    }


def _signup(code=SIGNUP):
    return {"Authorization": f"Bearer {code}"}


def test_self_serve_provisions_itself_with_the_signup_code(tmp_path):
    client = _app(tmp_path)
    r = client.post("/onboard", json=_spec(), headers=_signup())
    assert r.status_code == 201
    body = r.json()
    assert body["public_key"].startswith("pk_live_")   # auto-generated
    assert body["signing_secret"]                        # returned once to the customer
    assert body["customer_id"] == "acme"


def test_open_signup_needs_no_credential(tmp_path):
    client = _app(tmp_path)
    assert client.post("/onboard", json=_spec()).status_code == 201


def test_open_signup_is_create_only(tmp_path):
    """Signing up grants nothing over other tenants: the roster stays admin-only."""
    client = _app(tmp_path)
    client.post("/onboard", json=_spec("acme"))
    client.post("/onboard", json=_spec("globex"))
    assert client.get("/configs").status_code == 401


def test_open_signup_is_rate_limited_per_ip(tmp_path):
    client = _app(tmp_path)
    codes = [client.post("/onboard", json=_spec(f"t{i}")).status_code for i in range(6)]
    assert codes == [201] * 5 + [429]


def test_self_served_customer_is_live_immediately(tmp_path):
    import time
    from api.identity import sign_identity

    client = _app(tmp_path, outputs=[
        json.dumps({"action": "ask", "message": "What's prompting the cancellation?"}),
    ])
    created = client.post("/onboard", json=_spec(), headers=_signup()).json()
    key, secret = created["public_key"], created["signing_secret"]
    # The just-created key works on the very next request -- no restart. The tenant is secured
    # (a signing_secret is always minted), so a signed identity token is required.
    token = sign_identity(secret, {"user_id": "u1", "mrr": 49}, iat=int(time.time()))
    r = client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": token},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200


def test_self_serve_requires_own_claude_key(tmp_path):
    client = _app(tmp_path)
    spec = {k: v for k, v in _spec().items() if k != "anthropic_api_key"}
    r = client.post("/onboard", json=spec, headers=_signup())
    assert r.status_code == 422 and "Anthropic API key" in r.json()["detail"]
