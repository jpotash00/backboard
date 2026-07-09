"""Transcript privacy tests: user_id is pseudonymized in the logs, logs are date-partitioned,
and the pseudonymization is DETERMINISTIC so the resolution -> outcome join the flywheel needs
still lines up. Also the retention sweep and its horizon guard."""

from datetime import date

from engine import Interviewer, Outcome, UserContext
from eval.configs import ACME
from api import retention
from api.runs_io import partition_path, read_stream
from api.store import SessionState
from api.transcripts import TranscriptLogger


class _FakeModel:
    messages = None


def _state(user_id="real-user-42"):
    user = UserContext(user_id=user_id, plan="Growth", mrr=199, tenure_days=200,
                       logins_last_30d=12, activated=True, usage_summary="daily", signals={})
    interviewer = Interviewer(ACME, user, client=_FakeModel())
    return SessionState(
        session_id="sess1", customer_id="acme", interviewer=interviewer, config=ACME, user=user,
        transcript=[{"speaker": "interviewer", "text": "why?"}],
        outcome=Outcome(reason="price_value_mismatch", confidence=0.8, evidence="e",
                        cover_story="too_expensive", savable=True,
                        intervention_id="discount_50_3mo", rationale="r", turns_used=1),
        done=True, intended_intervention_id="discount_50_3mo",
    )


# --- Pseudonymization ----------------------------------------------------------------------

def test_raw_user_id_never_hits_the_log_when_pepper_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFBOARD_LOG_PEPPER", "pepper-value")
    logger = TranscriptLogger(directory=str(tmp_path))
    logger.log(_state())

    rec = read_stream(tmp_path, "sessions")[0]
    assert rec["user_id"] != "real-user-42"
    assert rec["user_id"].startswith("u_")
    assert rec["user_context"]["user_id"] == rec["user_id"]   # nested copy scrubbed too
    # the raw id appears nowhere in the serialized record
    import json
    assert "real-user-42" not in json.dumps(rec)


def test_pepson_off_by_default_keeps_raw_id(tmp_path, monkeypatch):
    monkeypatch.delenv("OFFBOARD_LOG_PEPPER", raising=False)
    logger = TranscriptLogger(directory=str(tmp_path))
    logger.log(_state())
    assert read_stream(tmp_path, "sessions")[0]["user_id"] == "real-user-42"


def test_pseudonymization_preserves_the_resolution_outcome_join(tmp_path, monkeypatch):
    """The whole constraint: hashing must be deterministic so a resolution row and its later
    outcome (reported with the RAW id from the billing backend) still share a join key."""
    monkeypatch.setenv("OFFBOARD_LOG_PEPPER", "pepper-value")
    logger = TranscriptLogger(directory=str(tmp_path))
    state = _state()
    logger.log_resolution(state, accepted=True)
    logger.log_outcome("acme", "real-user-42", active=True, observed_at="2026-08-01T00:00:00+00:00")

    res = read_stream(tmp_path, "resolutions")[0]
    out = read_stream(tmp_path, "outcomes")[0]
    assert res["user_id"] == out["user_id"]         # same hash -> join still works
    assert res["user_id"] != "real-user-42"


# --- Partitioning --------------------------------------------------------------------------

def test_writes_go_to_a_dated_partition(tmp_path):
    logger = TranscriptLogger(directory=str(tmp_path))
    logger.log(_state())
    files = list(tmp_path.glob("sessions-*.jsonl"))
    assert len(files) == 1
    assert not (tmp_path / "sessions.jsonl").exists()   # no monolithic file anymore


# --- Retention -----------------------------------------------------------------------------

def _write_partition(tmp_path, stem, day):
    p = partition_path(tmp_path, stem, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"x": 1}\n')
    return p


def test_prune_deletes_old_partitions_keeps_recent(tmp_path):
    today = date(2026, 7, 9)
    old = _write_partition(tmp_path, "sessions", date(2026, 1, 1))    # ~189 days old
    recent = _write_partition(tmp_path, "sessions", date(2026, 7, 1)) # 8 days old
    deleted = retention.prune(str(tmp_path), retention_days=90, now=today)
    assert old in deleted and not old.exists()
    assert recent not in deleted and recent.exists()


def test_prune_refuses_window_below_horizon_floor(tmp_path):
    # A 10-day window would drop resolutions before their 30d-horizon outcomes arrive.
    try:
        retention.prune(str(tmp_path), retention_days=10, now=date(2026, 7, 9))
        assert False, "expected a guard"
    except ValueError as e:
        assert "floor" in str(e)


def test_prune_force_overrides_the_floor(tmp_path):
    old = _write_partition(tmp_path, "outcomes", date(2026, 6, 1))
    deleted = retention.prune(str(tmp_path), retention_days=10, now=date(2026, 7, 9), force=True)
    assert old in deleted


def test_prune_leaves_legacy_monolithic_files_alone(tmp_path):
    legacy = tmp_path / "sessions.jsonl"     # no date -> can't be aged out, must survive
    legacy.write_text('{"x": 1}\n')
    retention.prune(str(tmp_path), retention_days=90, now=date(2030, 1, 1))
    assert legacy.exists()
