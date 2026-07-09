"""Security-boundary tests: signed identity, origin allowlist, rate limiting, input caps.

The threat model is a public publishable key (it ships in the browser) plus a hostile
browser. These tests pin the controls that make that safe: the browser cannot price its own
save, cannot drive the key from an unlisted origin, and cannot drain the model budget."""

import json

from fastapi.testclient import TestClient

from api.app import create_app
from api.identity import sign_identity
from api.registry import Customer, CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger
from eval.configs import ACME

SECRET = "shh_customer_signing_secret"
SECURED_KEY = "pk_secured"
NOW = 1_000_000  # fixed clock so token freshness is deterministic


class _Block:
    def __init__(self, text):
        self.text = text
        self.type = "text"


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class CapturingClient:
    """Always answers with one `ask`, and records the last system prompt so a test can assert
    which economics reached the model."""

    def __init__(self):
        self.last_system = None

    @property
    def messages(client_self):
        class _M:
            def create(_m, **kw):
                client_self.last_system = kw.get("system", "")
                return _Resp(json.dumps({"action": "ask", "message": "why leaving?"}))

        return _M()


def _system_text(system):
    return system if isinstance(system, str) else "".join(b["text"] for b in system)


def secured_app(tmp_path, allowed_origins=(), model=None):
    reg = CustomerRegistry()
    reg.register(
        Customer(
            id="secured",
            public_key=SECURED_KEY,
            config=ACME,
            signing_secret=SECRET,
            allowed_origins=tuple(allowed_origins),
        )
    )
    model = model or CapturingClient()
    app = create_app(
        registry=reg,
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: model,
        now=lambda: NOW,
    )
    return TestClient(app), model


AUTH = {"Authorization": f"Bearer {SECURED_KEY}"}


def _token(**claims):
    claims.setdefault("user_id", "u1")
    return sign_identity(SECRET, claims, iat=NOW)


# --- Signed identity -----------------------------------------------------------------------

def test_secured_key_requires_identity_token(tmp_path):
    client, _ = secured_app(tmp_path)
    r = client.post("/sessions", json={"user_id": "u1", "mrr": 500}, headers=AUTH)
    assert r.status_code == 401


def test_valid_token_opens_session(tmp_path):
    client, _ = secured_app(tmp_path)
    r = client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": _token(mrr=49, plan="Starter")},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["message"] == "why leaving?"


def test_spoofed_body_economics_are_ignored(tmp_path):
    """The browser posts a fat mrr; the signed token says 49. Policy must see 49."""
    client, model = secured_app(tmp_path)
    r = client.post(
        "/sessions",
        json={"user_id": "u1", "mrr": 99999, "identity_token": _token(mrr=49)},
        headers=AUTH,
    )
    assert r.status_code == 200
    text = _system_text(model.last_system)
    assert "49" in text
    assert "99999" not in text


def test_tampered_signature_is_rejected(tmp_path):
    client, _ = secured_app(tmp_path)
    token = _token(mrr=49)
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    r = client.post(
        "/sessions", json={"user_id": "u1", "identity_token": tampered}, headers=AUTH
    )
    assert r.status_code == 401


def test_forged_token_wrong_secret_is_rejected(tmp_path):
    client, _ = secured_app(tmp_path)
    forged = sign_identity("attacker_guess", {"user_id": "u1", "mrr": 99999}, iat=NOW)
    r = client.post(
        "/sessions", json={"user_id": "u1", "identity_token": forged}, headers=AUTH
    )
    assert r.status_code == 401


def test_expired_token_is_rejected(tmp_path):
    client, _ = secured_app(tmp_path)
    stale = sign_identity(SECRET, {"user_id": "u1", "mrr": 49}, iat=NOW - 601)
    r = client.post(
        "/sessions", json={"user_id": "u1", "identity_token": stale}, headers=AUTH
    )
    assert r.status_code == 401


# --- Origin allowlist ----------------------------------------------------------------------

def test_disallowed_origin_is_403(tmp_path):
    client, _ = secured_app(tmp_path, allowed_origins=["https://app.acme.com"])
    r = client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": _token(mrr=49)},
        headers={**AUTH, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_allowed_origin_passes(tmp_path):
    client, _ = secured_app(tmp_path, allowed_origins=["https://app.acme.com"])
    r = client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": _token(mrr=49)},
        headers={**AUTH, "Origin": "https://app.acme.com"},
    )
    assert r.status_code == 200


def test_no_origin_header_is_allowed(tmp_path):
    """Server-to-server calls carry no Origin; the allowlist only gates real browsers."""
    client, _ = secured_app(tmp_path, allowed_origins=["https://app.acme.com"])
    r = client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": _token(mrr=49)},
        headers=AUTH,
    )
    assert r.status_code == 200


# --- Rate limiting + input caps ------------------------------------------------------------

def test_session_creation_is_rate_limited(tmp_path):
    client, _ = secured_app(tmp_path)
    body = {"user_id": "u1", "identity_token": _token(mrr=49)}
    statuses = [
        client.post("/sessions", json=body, headers=AUTH).status_code for _ in range(25)
    ]
    assert 429 in statuses  # per-IP window (20/min) trips before 25


class _RecordingLimiter:
    """Captures every key the app checks, so a test can assert which IP the per-IP limit keyed
    off -- without having to drive the window to exhaustion."""

    def __init__(self):
        self.keys = []

    def check(self, key, limit, window_seconds):
        self.keys.append(key)


def test_per_ip_limit_keys_off_forwarded_ip_behind_proxy(tmp_path):
    """With a trusted proxy hop, the per-IP rate-limit bucket must be the real client from
    X-Forwarded-For, not the edge proxy the socket terminates at -- otherwise every user behind
    the proxy shares one bucket and the per-IP limit is inert."""
    reg = CustomerRegistry()
    reg.register(Customer(id="secured", public_key=SECURED_KEY, config=ACME,
                          signing_secret=SECRET))
    limiter = _RecordingLimiter()
    app = create_app(
        registry=reg,
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: CapturingClient(),
        now=lambda: NOW,
        limiter=limiter,
        trusted_proxy_hops=1,
    )
    client = TestClient(app)
    client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": _token(mrr=49)},
        headers={**AUTH, "X-Forwarded-For": "203.0.113.7"},
    )
    assert "session:ip:203.0.113.7" in limiter.keys
    # and the socket peer (TestClient's "testclient") is NOT what got limited
    assert not any(k.endswith(":ip:testclient") for k in limiter.keys)


def test_oversized_turn_message_is_422(tmp_path):
    client, _ = secured_app(tmp_path)
    sid = client.post(
        "/sessions",
        json={"user_id": "u1", "identity_token": _token(mrr=49)},
        headers=AUTH,
    ).json()["session_id"]
    r = client.post(
        f"/sessions/{sid}/turn", json={"user_message": "x" * 5000}, headers=AUTH
    )
    assert r.status_code == 422
