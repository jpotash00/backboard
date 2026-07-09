"""Per-tenant read/aggregate layer over the data asset -- the customer dashboard's backend.

Four streams feed the dashboard, all read through `runs_io.read_stream` and all filtered to a
single `customer_id` FIRST (the isolation boundary -- nothing here ever returns a row that isn't
the caller's; the endpoint layer separately proves the caller owns that id):

  - `resolutions` -> the aggregate readout (`aggregate`): reason mix, save rate, retained MRR,
    per-reason / per-offer funnels, and a daily `timeseries`.
  - `sessions`    -> the session list (`recent_sessions`) and the FULL decision inspection
    (`session_detail`): transcript + the complete audit trail policy wrote onto the outcome
    (decision_trace, economics, corroboration, observations). This is the "how did it decide"
    surface. Acceptance (`accepted`) is joined from `resolutions` by session_id.
  - `events`      -> the operational activity/failure log (`recent_activity`).

Reads hit disk per call. Fine at dashboard-tier volume; a busy tenant is the trigger to cache or
move behind a DB (the metering seam already flagged in api.transcripts), not to change this shape.
"""

from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional, Union

from .runs_io import read_stream

# resolution-row fields safe to surface. Omits the pseudonymized user_id (aggregate-first).
_EVENT_FIELDS = ("logged_at", "session_id", "reason", "mode", "intervention_type", "offered", "accepted", "mrr")


def _rows_for(directory, stem: str, customer_id: str,
              since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    """Every row in a stream belonging to exactly this customer -- the isolation boundary --
    optionally restricted to a [since, until) time window. `logged_at` is ISO-8601 UTC, so the
    string bounds (whole dates or full timestamps) compare correctly with a lexical test."""
    out = [r for r in read_stream(directory, stem) if r.get("customer_id") == customer_id]
    if since is not None:
        out = [r for r in out if (r.get("logged_at") or "") >= since]
    if until is not None:
        out = [r for r in out if (r.get("logged_at") or "") < until]
    return out


def _mrr(row: dict) -> float:
    v = row.get("mrr")
    return float(v) if isinstance(v, (int, float)) else 0.0


# --- Aggregate readout (resolutions) ------------------------------------------------------

def aggregate(directory: Union[str, Path], customer_id: str,
              since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Rolled-up churn intelligence for one tenant over an optional [since, until) window: the real
    reasons, the saves won, the MRR retained, plus a daily timeseries for the activity chart.

    `save_rate` is OBSERVATIONAL (accepted / offered) -- honest, not causal; the holdout lift is a
    later tier. `retained_mrr` sums the MRR of accepted offers (gross, not net of counterfactual)."""
    rows = _rows_for(directory, "resolutions", customer_id, since, until)
    offered = [r for r in rows if r.get("offered")]
    accepted = [r for r in offered if r.get("accepted")]

    reason_counts = Counter(r.get("reason") for r in rows if r.get("reason"))

    by_reason: dict[str, dict] = {}
    for r in rows:
        reason = r.get("reason")
        if not reason:
            continue
        cell = by_reason.setdefault(reason, {"sessions": 0, "offered": 0, "accepted": 0, "retained_mrr": 0.0})
        cell["sessions"] += 1
        if r.get("offered"):
            cell["offered"] += 1
            if r.get("accepted"):
                cell["accepted"] += 1
                cell["retained_mrr"] += _mrr(r)

    by_type: dict[str, dict] = {}
    for r in offered:
        itype = r.get("intervention_type") or "unknown"
        cell = by_type.setdefault(itype, {"offered": 0, "accepted": 0, "retained_mrr": 0.0})
        cell["offered"] += 1
        if r.get("accepted"):
            cell["accepted"] += 1
            cell["retained_mrr"] += _mrr(r)

    # Daily timeseries: sessions / at-risk MRR / saves / retained MRR, ascending by date -- the
    # substrate for the activity chart and the KPI sparklines.
    days: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "offered": 0, "accepted": 0, "at_risk": 0.0, "retained_mrr": 0.0})
    for r in rows:
        day = (r.get("logged_at") or "")[:10]
        if not day:
            continue
        d = days[day]
        d["sessions"] += 1
        d["at_risk"] += _mrr(r)
        if r.get("offered"):
            d["offered"] += 1
            if r.get("accepted"):
                d["accepted"] += 1
                d["retained_mrr"] += _mrr(r)
    timeseries = [{"date": k, **v} for k, v in sorted(days.items())]

    # Retention economics -- the numbers a cancel-flow buyer actually cares about.
    #   at_risk_mrr    -- MRR of every account that entered the cancel flow (what was on the line).
    #   recovered_mrr  -- MRR of the accounts a save offer retained (== retained_mrr).
    #   deflection_rate-- of ALL cancel attempts, the share the engine saved (saves / sessions).
    #   recovery_rate  -- share of at-risk MRR that was recovered.
    at_risk_mrr = round(sum(_mrr(r) for r in rows), 2)
    recovered_mrr = round(sum(_mrr(r) for r in accepted), 2)
    total = len(rows)
    retention = _retention_of_saves(directory, customer_id, accepted)

    return {
        "customer_id": customer_id,
        "total_sessions": total,
        "offers_made": len(offered),
        "offers_accepted": len(accepted),
        "save_rate": (len(accepted) / len(offered)) if offered else None,
        "deflection_rate": (len(accepted) / total) if total else None,
        "at_risk_mrr": at_risk_mrr,
        "retained_mrr": recovered_mrr,
        "recovery_rate": (recovered_mrr / at_risk_mrr) if at_risk_mrr else None,
        "retention": retention,
        "reason_breakdown": dict(reason_counts.most_common()),
        "by_reason": by_reason,
        "by_intervention_type": by_type,
        "timeseries": timeseries,
    }


def _retention_of_saves(directory, customer_id: str, accepted: list[dict]) -> dict:
    """Durable retention: of the users a save offer retained, how many were STILL subscribed at the
    customer's last retention check (POST /outcomes -> outcomes stream). This is the ground-truth
    'did the save stick?' metric -- gross save rate can look great while the saved users churn a
    month later; this is the honest counterweight.

    `checked` is the denominator (saved users we have any outcome for); users with no reported
    outcome are excluded rather than assumed retained. `rate` is None until there's data to judge."""
    saved_users = {r.get("user_id") for r in accepted if r.get("user_id")}
    if not saved_users:
        return {"checked": 0, "retained": 0, "rate": None}
    # Latest observation per user (outcomes are append-only; several checks over time are fine).
    latest: dict[str, dict] = {}
    for o in _rows_for(directory, "outcomes", customer_id):
        uid = o.get("user_id")
        if uid in saved_users and (uid not in latest or (o.get("observed_at") or "") >= (latest[uid].get("observed_at") or "")):
            latest[uid] = o
    retained = sum(1 for o in latest.values() if o.get("active"))
    checked = len(latest)
    return {"checked": checked, "retained": retained, "rate": (retained / checked) if checked else None}


# --- Sessions (sessions stream, joined to resolutions) ------------------------------------

def _resolution_index(directory, customer_id: str) -> dict[str, dict]:
    """session_id -> its resolution row (accepted / intervention_type / mrr), for joining onto
    the session list and detail. A session with no resolution (user closed the modal) has no
    entry -- its result is 'pending'."""
    return {r["session_id"]: r for r in _rows_for(directory, "resolutions", customer_id) if r.get("session_id")}


def recent_sessions(directory: Union[str, Path], customer_id: str, limit: int = 50,
                    since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    """The tenant's completed interviews in the window, newest first, projected to a summary row.
    Result joins acceptance from the resolutions stream: saved / declined / no_offer / pending."""
    res = _resolution_index(directory, customer_id)
    rows = _rows_for(directory, "sessions", customer_id, since, until)
    rows.sort(key=lambda r: r.get("logged_at") or "", reverse=True)
    out = []
    for r in rows[: max(0, limit)]:
        o = r.get("outcome") or {}
        uc = r.get("user_context") or {}
        rr = res.get(r.get("session_id"))
        offered = bool(o.get("intervention_id"))
        accepted = rr.get("accepted") if rr else None
        result = "no_offer" if not offered else ("saved" if accepted else "declined" if rr else "pending")
        out.append({
            "logged_at": r.get("logged_at"),
            "session_id": r.get("session_id"),
            "reason": o.get("reason"),
            "cover_story": o.get("cover_story"),
            "mode": o.get("mode"),
            "confidence": o.get("confidence"),
            "intervention_type": (rr or {}).get("intervention_type"),
            "offered": offered,
            "accepted": accepted,
            "result": result,
            "mrr": uc.get("mrr"),
            "plan": uc.get("plan"),
            "arm": r.get("arm"),
        })
    return out


def session_detail(directory: Union[str, Path], customer_id: str, session_id: str) -> Optional[dict]:
    """The FULL inspection record for one session: the transcript and the complete decision audit
    trail policy wrote onto the outcome. Returns None if no such session belongs to this customer
    (the customer_id match is the isolation gate -- a caller can't read another tenant's session
    by guessing an id)."""
    row = next(
        (r for r in read_stream(directory, "sessions")
         if r.get("session_id") == session_id and r.get("customer_id") == customer_id),
        None,
    )
    if row is None:
        return None
    o = row.get("outcome") or {}
    rr = _resolution_index(directory, customer_id).get(session_id)
    return {
        "session_id": row.get("session_id"),
        "logged_at": row.get("logged_at"),
        "customer_id": row.get("customer_id"),
        "arm": row.get("arm"),
        "user_context": row.get("user_context") or {},
        "transcript": row.get("transcript") or [],
        # The diagnosis + the full authorization audit trail, exactly as the engine recorded it.
        "outcome": {
            "reason": o.get("reason"),
            "cover_story": o.get("cover_story"),
            "confidence": o.get("confidence"),
            "evidence": o.get("evidence"),
            "savable": o.get("savable"),
            "mode": o.get("mode"),
            "intervention_id": o.get("intervention_id"),
            "rationale": o.get("rationale"),
            "turns_used": o.get("turns_used"),
            "economics": o.get("economics"),
            "decision_trace": o.get("decision_trace") or [],
            "corroboration": o.get("corroboration"),
            "observations": o.get("observations") or {},
        },
        "resolution": ({"offered": rr.get("offered"), "accepted": rr.get("accepted"),
                        "intervention_type": rr.get("intervention_type")} if rr else None),
    }


# --- Operational activity (events stream) -------------------------------------------------

def recent_activity(directory: Union[str, Path], customer_id: str, limit: int = 100,
                    since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    """The tenant's operational log -- failures and notable actions, newest first. This is the
    'is my cancel flow healthy?' view (rate limits, rejected identity tokens, model errors,
    expired sessions), separate from the churn data."""
    rows = _rows_for(directory, "events", customer_id, since, until)
    rows.sort(key=lambda r: r.get("logged_at") or "", reverse=True)
    return [
        {"logged_at": r.get("logged_at"), "type": r.get("type"), "detail": r.get("detail"),
         "session_id": r.get("session_id"), "meta": r.get("meta") or {}}
        for r in rows[: max(0, limit)]
    ]


# --- Causal lift (the holdout read) -------------------------------------------------------

def causal_lift(directory: Union[str, Path], customer_id: str,
                since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """The differentiated metric: did the save flow actually CAUSE retention, or would those users
    have stayed anyway? Every interviewed user is randomized to an arm -- `treatment` gets the
    decided offer, `control` (the holdout) gets none. Comparing retention between arms isolates the
    causal effect of the intervention; a raw save rate can't (you only ever see the users you saved).

    Retention is joined from the outcomes stream (latest observation per user, all-time -- a horizon
    check naturally lands after the session window, so outcomes are NOT windowed). Lift is the
    percentage-point gap in retention; `incremental_mrr` applies that gap to the treated MRR base --
    revenue kept that the holdout implies you would otherwise have lost. Gross, and honest about its
    denominators: `sufficient` is False until BOTH arms have enough checked users to compare."""
    sessions = _rows_for(directory, "sessions", customer_id, since, until)

    latest: dict[str, dict] = {}
    for o in _rows_for(directory, "outcomes", customer_id):
        uid = o.get("user_id")
        if uid and (uid not in latest or (o.get("observed_at") or "") >= (latest[uid].get("observed_at") or "")):
            latest[uid] = o

    arms: dict[str, dict] = {}
    for s in sessions:
        arm = s.get("arm") or "treatment"
        uc = s.get("user_context") or {}
        uid = uc.get("user_id") or s.get("user_id")
        cell = arms.setdefault(arm, {"users": 0, "checked": 0, "retained": 0, "mrr": 0.0})
        cell["users"] += 1
        cell["mrr"] += _mrr({"mrr": uc.get("mrr")})
        o = latest.get(uid)
        if o is not None:
            cell["checked"] += 1
            if o.get("active"):
                cell["retained"] += 1

    def rate(cell: Optional[dict]) -> Optional[float]:
        return (cell["retained"] / cell["checked"]) if cell and cell["checked"] else None

    t, c = arms.get("treatment"), arms.get("control")
    rate_t, rate_c = rate(t), rate(c)
    lift = round(rate_t - rate_c, 4) if (rate_t is not None and rate_c is not None) else None
    incremental_mrr = round(lift * t["mrr"], 2) if (lift is not None and t) else None
    sufficient = bool(t and c and t["checked"] and c["checked"])

    def arm_out(cell: Optional[dict]) -> dict:
        cell = cell or {"users": 0, "checked": 0, "retained": 0, "mrr": 0.0}
        return {"users": cell["users"], "checked": cell["checked"], "retained": cell["retained"],
                "retention_rate": rate(cell), "mrr": round(cell["mrr"], 2)}

    return {
        "treatment": arm_out(t),
        "control": arm_out(c),
        "lift_pts": lift,                 # percentage-point retention gain from the offer
        "incremental_mrr": incremental_mrr,
        "sufficient": sufficient,         # False -> not enough holdout data to trust the read yet
    }
