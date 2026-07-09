"""Customer insights: the dashboard's read API and the pure aggregation/inspection in api.insights.

Properties that matter:
  - the aggregate is arithmetically right (save rate, retained MRR, funnels, daily timeseries);
  - the session list joins acceptance and the detail exposes the FULL decision audit trail;
  - failures are logged to the activity stream and surfaced per tenant;
  - ISOLATION everywhere: a tenant's readout contains only its own rows, one tenant's credential
    can never read another's, and reads are gated by the SECRET, never the public publishable key.
"""

import json

from fastapi.testclient import TestClient

from api.app import create_app
from api.insights import aggregate, causal_lift, recent_activity, recent_sessions, session_detail
from api.registry import CustomerRegistry
from api.store import SessionStore
from api.transcripts import TranscriptLogger

ADMIN = "sk_admin_secret"
SIGNUP = "signup_code"


def _res(customer_id, reason, offered, accepted, itype, mrr, logged_at, session_id):
    return {"logged_at": logged_at, "session_id": session_id, "customer_id": customer_id,
            "user_id": "u_hash", "mrr": mrr, "reason": reason, "mode": "act",
            "intervention_id": (itype + "_1") if itype else None, "intervention_type": itype,
            "offered": offered, "accepted": accepted}


def _sess(customer_id, session_id, reason, itype, logged_at, mrr=49, arm="treatment"):
    """A sessions.jsonl record carrying the full decision audit trail."""
    iv_id = (itype + "_1") if itype else None
    trace = ([{"type": itype, "rank": 0, "intervention_id": iv_id, "cost": 12.0,
               "expected_value": 88.0, "eligible": True, "chosen": True},
              {"type": "downgrade", "rank": 1, "eligible": False, "rejected": "ineligible (gate)"}]
             if itype else [{"decision": "defer", "gate": "confidence_floor",
                             "effective_confidence": 0.4, "floor": 0.6}])
    return {
        "logged_at": logged_at, "session_id": session_id, "customer_id": customer_id, "user_id": "u_hash",
        "user_context": {"user_id": "u_hash", "mrr": mrr, "plan": "Growth", "tenure_days": 210},
        "transcript": [{"speaker": "interviewer", "text": "what's prompting this?"},
                       {"speaker": "churner", "text": "too pricey"}],
        "outcome": {
            "reason": reason, "confidence": 0.82, "evidence": "hit the event cap repeatedly",
            "cover_story": "too expensive", "savable": bool(itype), "intervention_id": iv_id,
            "rationale": f"{reason} -> {itype}", "turns_used": 3, "mode": "act",
            "economics": {"customer_value": mrr * 12, "margin_spent": 12.0, "chosen_expected_value": 88.0},
            "decision_trace": trace,
            "corroboration": {"status": "corroborated", "rule": "logins > 10", "raw_confidence": 0.82,
                              "penalty": 0.0, "effective_confidence": 0.82},
            "observations": {"competitor": None, "acceptable_price": 29},
        },
        "arm": arm, "intended_intervention_id": iv_id,
    }


def _write(directory, stem, rows):
    with (directory / f"{stem}.jsonl").open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _seed(directory):
    _write(directory, "resolutions", [
        _res("acme", "price_value_mismatch", True, True, "discount", 49, "2026-07-01T10:00:00+00:00", "s1"),
        _res("acme", "price_value_mismatch", True, False, "discount", 49, "2026-07-01T12:00:00+00:00", "s2"),
        _res("acme", "missing_feature", True, True, "roadmap", 199, "2026-07-03T10:00:00+00:00", "s3"),
        _res("acme", "never_activated", False, False, None, 19, "2026-07-04T10:00:00+00:00", "s4"),
        _res("globex", "too_expensive", True, True, "discount", 99, "2026-07-05T10:00:00+00:00", "sg"),
    ])


# --- Aggregate ----------------------------------------------------------------------------

def test_aggregate_headline_numbers(tmp_path):
    _seed(tmp_path)
    a = aggregate(tmp_path, "acme")
    assert (a["total_sessions"], a["offers_made"], a["offers_accepted"]) == (4, 3, 2)
    assert a["save_rate"] == 2 / 3
    assert a["retained_mrr"] == 248.0


def test_aggregate_timeseries_buckets_by_day(tmp_path):
    _seed(tmp_path)
    ts = {d["date"]: d for d in aggregate(tmp_path, "acme")["timeseries"]}
    assert ts["2026-07-01"]["sessions"] == 2 and ts["2026-07-01"]["accepted"] == 1
    assert ts["2026-07-03"]["retained_mrr"] == 199.0
    assert list(ts.keys()) == sorted(ts.keys())  # ascending


def test_aggregate_retention_economics(tmp_path):
    _seed(tmp_path)  # acme: 4 sessions, 3 offered, 2 accepted (mrr 49 + 199), at-risk = 49+49+199+19
    a = aggregate(tmp_path, "acme")
    assert a["at_risk_mrr"] == 316.0
    assert a["retained_mrr"] == 248.0
    assert a["deflection_rate"] == 2 / 4          # saves / all cancel attempts
    assert abs(a["recovery_rate"] - 248.0 / 316.0) < 1e-9


def test_aggregate_durable_retention_from_outcomes(tmp_path):
    """Retention joins the outcomes stream: of saved users, how many were still active at the last
    check. s1 (u_a) saved & still active; s3 (u_c) saved but later churned -> 1/2 retained."""
    _write(tmp_path, "resolutions", [
        {**_res("acme", "price_value_mismatch", True, True, "discount", 49, "2026-07-01T10:00:00+00:00", "s1"), "user_id": "u_a"},
        {**_res("acme", "missing_feature", True, True, "roadmap", 199, "2026-07-03T10:00:00+00:00", "s3"), "user_id": "u_c"},
    ])
    _write(tmp_path, "outcomes", [
        {"logged_at": "2026-07-30T00:00:00+00:00", "customer_id": "acme", "user_id": "u_a", "active": True, "observed_at": "2026-07-30"},
        {"logged_at": "2026-07-30T00:00:00+00:00", "customer_id": "acme", "user_id": "u_c", "active": False, "observed_at": "2026-07-30"},
    ])
    ret = aggregate(tmp_path, "acme")["retention"]
    assert ret == {"checked": 2, "retained": 1, "rate": 0.5}


def test_aggregate_empty_is_safe(tmp_path):
    a = aggregate(tmp_path, "nobody")
    assert a["total_sessions"] == 0 and a["save_rate"] is None and a["timeseries"] == []
    assert a["at_risk_mrr"] == 0 and a["retention"] == {"checked": 0, "retained": 0, "rate": None}


def test_aggregate_date_window_filters(tmp_path):
    _seed(tmp_path)  # acme rows on 07-01 (x2), 07-03, 07-04
    windowed = aggregate(tmp_path, "acme", since="2026-07-03T00:00:00+00:00")
    assert windowed["total_sessions"] == 2   # only 07-03 and 07-04 rows
    until_only = aggregate(tmp_path, "acme", until="2026-07-02T00:00:00+00:00")
    assert until_only["total_sessions"] == 2  # only the two 07-01 rows


# --- Causal lift --------------------------------------------------------------------------

def test_causal_lift_treatment_beats_control(tmp_path):
    """Treatment retention 100% (u_t active) vs control 0% (u_c churned) -> +1.0 lift."""
    _write(tmp_path, "sessions", [
        {**_sess("acme", "s1", "price_value_mismatch", "discount", "2026-07-01T10:00:00+00:00", mrr=100, arm="treatment"),
         "user_context": {"user_id": "u_t", "mrr": 100}},
        {**_sess("acme", "s2", "price_value_mismatch", "discount", "2026-07-01T11:00:00+00:00", mrr=100, arm="control"),
         "user_context": {"user_id": "u_c", "mrr": 100}},
    ])
    _write(tmp_path, "outcomes", [
        {"customer_id": "acme", "user_id": "u_t", "active": True, "observed_at": "2026-08-01"},
        {"customer_id": "acme", "user_id": "u_c", "active": False, "observed_at": "2026-08-01"},
    ])
    c = causal_lift(tmp_path, "acme")
    assert c["treatment"]["retention_rate"] == 1.0 and c["control"]["retention_rate"] == 0.0
    assert c["lift_pts"] == 1.0 and c["sufficient"] is True
    assert c["incremental_mrr"] == 100.0   # lift(1.0) x treated mrr base(100)


def test_causal_lift_insufficient_without_both_arms(tmp_path):
    _write(tmp_path, "sessions", [_sess("acme", "s1", "price_value_mismatch", "discount", "2026-07-01T10:00:00+00:00", arm="treatment")])
    c = causal_lift(tmp_path, "acme")
    assert c["sufficient"] is False and c["lift_pts"] is None


# --- Sessions list + detail ---------------------------------------------------------------

def test_recent_sessions_join_result_and_order(tmp_path):
    _seed(tmp_path)
    _write(tmp_path, "sessions", [
        _sess("acme", "s1", "price_value_mismatch", "discount", "2026-07-01T10:00:00+00:00"),
        _sess("acme", "s2", "price_value_mismatch", "discount", "2026-07-01T12:00:00+00:00"),
        _sess("acme", "s5", "missing_feature", None, "2026-07-06T10:00:00+00:00"),  # no offer, no resolution
    ])
    rows = recent_sessions(tmp_path, "acme", 50)
    assert [r["session_id"] for r in rows] == ["s5", "s2", "s1"]  # newest first
    by_id = {r["session_id"]: r for r in rows}
    assert by_id["s1"]["result"] == "saved"       # offered + accepted
    assert by_id["s2"]["result"] == "declined"     # offered + not accepted
    assert by_id["s5"]["result"] == "no_offer"     # no intervention decided


def test_session_detail_exposes_full_trace(tmp_path):
    _write(tmp_path, "sessions", [_sess("acme", "s1", "price_value_mismatch", "discount", "2026-07-01T10:00:00+00:00")])
    _write(tmp_path, "resolutions", [_res("acme", "price_value_mismatch", True, True, "discount", 49, "2026-07-01T10:00:00+00:00", "s1")])
    d = session_detail(tmp_path, "acme", "s1")
    assert d["outcome"]["cover_story"] == "too expensive" and d["outcome"]["reason"] == "price_value_mismatch"
    assert d["outcome"]["decision_trace"][0]["chosen"] is True
    assert d["outcome"]["corroboration"]["effective_confidence"] == 0.82
    assert d["outcome"]["economics"]["chosen_expected_value"] == 88.0
    assert len(d["transcript"]) == 2
    assert d["resolution"]["accepted"] is True


def test_session_detail_isolation(tmp_path):
    """A session belonging to globex must be invisible when read under acme."""
    _write(tmp_path, "sessions", [_sess("globex", "sg", "too_expensive", "discount", "2026-07-05T10:00:00+00:00")])
    assert session_detail(tmp_path, "acme", "sg") is None       # not acme's
    assert session_detail(tmp_path, "globex", "sg") is not None  # is globex's


def test_recent_activity_reads_events(tmp_path):
    _write(tmp_path, "events", [
        {"logged_at": "2026-07-01T10:00:00+00:00", "customer_id": "acme", "type": "rate_limited", "detail": "turn rate limit hit", "session_id": None, "meta": {}},
        {"logged_at": "2026-07-02T10:00:00+00:00", "customer_id": "acme", "type": "model_error", "detail": "Timeout", "session_id": "s9", "meta": {}},
        {"logged_at": "2026-07-03T10:00:00+00:00", "customer_id": "globex", "type": "rate_limited", "detail": "x", "session_id": None, "meta": {}},
    ])
    acts = recent_activity(tmp_path, "acme", 50)
    assert [a["type"] for a in acts] == ["model_error", "rate_limited"]  # newest first, globex excluded


# --- Endpoints: auth + isolation ----------------------------------------------------------

def _client(tmp_path):
    return TestClient(create_app(
        registry=CustomerRegistry(), store=SessionStore(),
        logger=TranscriptLogger(directory=str(tmp_path)), client_factory=lambda: None,
        admin_key=ADMIN, signup_key=SIGNUP, config_dir=str(tmp_path / "configs")))


def _provision(client, customer_id):
    spec = {"customer_id": customer_id,
            "product": {"product_name": "Acme", "product_context": "analytics",
                        "activation_definition": "connected a source", "pricing_summary": "$49 / $199"},
            "offers": [{"type": "discount", "description": "50% off for 3 months"}]}
    return client.post("/onboard", json=spec, headers={"Authorization": f"Bearer {SIGNUP}"}).json()


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def test_owner_reads_analytics_with_timeseries(tmp_path):
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    _write(tmp_path, "resolutions", [_res("acme", "price_value_mismatch", True, True, "discount", 49, "2026-07-01T10:00:00+00:00", "s1")])
    r = c.get("/insights/acme/analytics", headers=_bearer(acme["signing_secret"]))
    assert r.status_code == 200 and r.json()["retained_mrr"] == 49.0
    assert r.json()["timeseries"][0]["date"] == "2026-07-01"


def test_session_detail_endpoint_and_isolation(tmp_path):
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    globex = _provision(c, "globex")
    _write(tmp_path, "sessions", [_sess("acme", "s1", "price_value_mismatch", "discount", "2026-07-01T10:00:00+00:00")])
    # owner reads full detail
    r = c.get("/insights/acme/sessions/s1", headers=_bearer(acme["signing_secret"]))
    assert r.status_code == 200 and r.json()["outcome"]["decision_trace"][0]["chosen"] is True
    # another tenant is rejected by auth (401), never even reaching the row
    assert c.get("/insights/acme/sessions/s1", headers=_bearer(globex["signing_secret"])).status_code == 401
    # admin reading a session id that isn't acme's -> 404 (isolation in the read layer)
    assert c.get("/insights/acme/sessions/does_not_exist", headers=_bearer(ADMIN)).status_code == 404


def test_publishable_key_cannot_read_any_insight(tmp_path):
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    pk = _bearer(acme["public_key"])
    for path in ("/insights/acme/analytics", "/insights/acme/sessions", "/insights/acme/activity"):
        assert c.get(path, headers=pk).status_code == 401


def test_failure_is_logged_and_surfaced_in_activity(tmp_path):
    """A turn on an unknown session 404s AND lands in the tenant's activity log."""
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    pk = acme["public_key"]
    r = c.post("/sessions/ghost_session/turn", json={"user_message": "hi"}, headers=_bearer(pk))
    assert r.status_code == 404
    acts = c.get("/insights/acme/activity", headers=_bearer(acme["signing_secret"])).json()["activity"]
    assert any(a["type"] == "session_not_found" and a["session_id"] == "ghost_session" for a in acts)


def test_analytics_endpoint_has_range_and_previous(tmp_path):
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    a = c.get("/insights/acme/analytics?days=30", headers=_bearer(acme["signing_secret"])).json()
    assert a["range_days"] == 30 and "previous" in a          # period-over-period present
    a0 = c.get("/insights/acme/analytics?days=0", headers=_bearer(acme["signing_secret"])).json()
    assert a0["range_days"] == 0 and "previous" not in a0      # all-time has no prior window


def test_causal_endpoint_isolation_and_auth(tmp_path):
    c = _client(tmp_path)
    acme = _provision(c, "acme")
    globex = _provision(c, "globex")
    assert c.get("/insights/acme/causal", headers=_bearer(acme["signing_secret"])).status_code == 200
    assert c.get("/insights/acme/causal", headers=_bearer(globex["signing_secret"])).status_code == 401
    assert c.get("/insights/acme/causal", headers=_bearer(acme["public_key"])).status_code == 401


def test_missing_credential_is_401(tmp_path):
    c = _client(tmp_path)
    _provision(c, "acme")
    assert c.get("/insights/acme/analytics").status_code == 401
    assert c.get("/insights/acme/sessions/s1").status_code == 401
