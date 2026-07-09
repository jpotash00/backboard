"""Customer insights: the dashboard's read API (GET /insights/{id}/analytics and /events) plus the
pure aggregation in api.insights.

The properties that matter:
  - the aggregate is arithmetically right (save rate, retained MRR, per-reason / per-type funnels);
  - events are projected to customer-safe fields and never carry the pseudonymized user_id;
  - ISOLATION: a tenant's readout contains only its own rows, and one tenant's credential can never
    read another's -- the read must be gated by the SECRET (signing_secret / admin), never the
    public publishable key.
"""

import json

from fastapi.testclient import TestClient

from api.app import create_app
from api.insights import aggregate, recent_events
from api.registry import CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger

ADMIN = "sk_admin_secret"
SIGNUP = "signup_code"


def _row(customer_id, reason, offered, accepted, itype, mrr, logged_at, user_id="u_hash"):
    return {
        "logged_at": logged_at,
        "session_id": f"s_{logged_at}",
        "customer_id": customer_id,
        "user_id": user_id,
        "mrr": mrr,
        "reason": reason,
        "mode": "act",
        "intervention_id": (itype + "_1") if itype else None,
        "intervention_type": itype,
        "offered": offered,
        "accepted": accepted,
    }


def _write(directory, rows):
    """Append resolution rows to the legacy monolithic partition, which read_stream also reads --
    keeps the fixture independent of today's date."""
    path = directory / "resolutions.jsonl"
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# Two tenants' worth of resolutions; acme is the subject, globex is the isolation control.
def _seed(directory):
    _write(directory, [
        _row("acme", "price_value_mismatch", True, True, "discount", 49, "2026-07-01T10:00:00+00:00"),
        _row("acme", "price_value_mismatch", True, False, "discount", 49, "2026-07-02T10:00:00+00:00"),
        _row("acme", "missing_feature", True, True, "roadmap", 199, "2026-07-03T10:00:00+00:00"),
        _row("acme", "never_activated", False, False, None, 19, "2026-07-04T10:00:00+00:00"),
        _row("globex", "too_expensive", True, True, "discount", 99, "2026-07-05T10:00:00+00:00"),
    ])


# --- Pure aggregation ---------------------------------------------------------------------

def test_aggregate_headline_numbers(tmp_path):
    _seed(tmp_path)
    a = aggregate(tmp_path, "acme")
    assert a["total_sessions"] == 4          # globex excluded
    assert a["offers_made"] == 3
    assert a["offers_accepted"] == 2
    assert a["save_rate"] == 2 / 3
    assert a["retained_mrr"] == 248.0        # 49 + 199 (the two accepted offers)


def test_aggregate_reason_and_type_funnels(tmp_path):
    _seed(tmp_path)
    a = aggregate(tmp_path, "acme")
    assert a["reason_breakdown"] == {"price_value_mismatch": 2, "missing_feature": 1, "never_activated": 1}
    pv = a["by_reason"]["price_value_mismatch"]
    assert (pv["sessions"], pv["offered"], pv["accepted"], pv["retained_mrr"]) == (2, 2, 1, 49.0)
    na = a["by_reason"]["never_activated"]
    assert (na["offered"], na["accepted"]) == (0, 0)  # a control row: counted, but no offer
    disc = a["by_intervention_type"]["discount"]
    assert (disc["offered"], disc["accepted"], disc["retained_mrr"]) == (2, 1, 49.0)


def test_aggregate_empty_is_safe(tmp_path):
    a = aggregate(tmp_path, "nobody")
    assert a["total_sessions"] == 0 and a["save_rate"] is None and a["retained_mrr"] == 0


def test_events_newest_first_and_no_user_id(tmp_path):
    _seed(tmp_path)
    events = recent_events(tmp_path, "acme", limit=50)
    assert len(events) == 4
    assert [e["logged_at"] for e in events] == sorted([e["logged_at"] for e in events], reverse=True)
    assert all("user_id" not in e for e in events)  # aggregate-first: never leak the pseudonym
    assert {e["reason"] for e in events} == {"price_value_mismatch", "missing_feature", "never_activated"}


def test_events_respect_limit(tmp_path):
    _seed(tmp_path)
    assert len(recent_events(tmp_path, "acme", limit=2)) == 2


# --- Endpoint: auth + isolation -----------------------------------------------------------

def _client(tmp_path):
    return TestClient(create_app(
        registry=CustomerRegistry(),
        store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)),
        client_factory=lambda: None,
        admin_key=ADMIN,
        signup_key=SIGNUP,
        config_dir=str(tmp_path / "configs"),
    ))


def _provision(client, customer_id):
    spec = {
        "customer_id": customer_id,
        "product": {
            "product_name": "Acme",
            "product_context": "analytics",
            "activation_definition": "connected a source",
            "pricing_summary": "$49 / $199",
        },
        "offers": [{"type": "discount", "description": "50% off for 3 months"}],
    }
    return client.post("/onboard", json=spec, headers={"Authorization": f"Bearer {SIGNUP}"}).json()


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def test_owner_reads_own_analytics_with_signing_secret(tmp_path):
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    _write(tmp_path, [_row("acme", "price_value_mismatch", True, True, "discount", 49, "2026-07-01T10:00:00+00:00")])
    r = c.get("/insights/acme/analytics", headers=_bearer(acme["signing_secret"]))
    assert r.status_code == 200
    assert r.json()["retained_mrr"] == 49.0


def test_admin_can_read_any_tenant(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    r = c.get("/insights/acme/analytics", headers=_bearer(ADMIN))
    assert r.status_code == 200


def test_publishable_key_cannot_read_insights(tmp_path):
    """The public browser key must NOT unlock the private dashboard -- else anyone who saw the pk_
    in a page could read the tenant's churn data."""
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    r = c.get("/insights/acme/analytics", headers=_bearer(acme["public_key"]))
    assert r.status_code == 401


def test_one_tenant_cannot_read_another(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    globex = _provision(c, "globex")
    # globex's real secret must not read acme's insights...
    assert c.get("/insights/acme/analytics", headers=_bearer(globex["signing_secret"])).status_code == 401
    # ...nor a wrong secret.
    assert c.get("/insights/acme/events", headers=_bearer("not_the_secret")).status_code == 401


def test_endpoint_returns_only_the_callers_rows(tmp_path):
    """Even the admin reading /insights/acme sees ONLY acme's rows -- the read layer filters by id."""
    c = _client(tmp_path)
    _provision(c, "acme")
    _write(tmp_path, [
        _row("acme", "missing_feature", True, True, "roadmap", 199, "2026-07-03T10:00:00+00:00"),
        _row("globex", "too_expensive", True, True, "discount", 99, "2026-07-05T10:00:00+00:00"),
    ])
    a = c.get("/insights/acme/analytics", headers=_bearer(ADMIN)).json()
    assert a["total_sessions"] == 1 and a["retained_mrr"] == 199.0  # globex's 99 is not visible
    events = c.get("/insights/acme/events", headers=_bearer(ADMIN)).json()["events"]
    assert all(e["reason"] != "too_expensive" for e in events)


def test_missing_credential_is_401(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    assert c.get("/insights/acme/analytics").status_code == 401
