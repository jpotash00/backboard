"""Per-tenant read/aggregate layer over the resolutions data asset -- the customer-facing
dashboard's backend.

The write side (`api.transcripts`) already banks one self-contained record per session outcome
into the `resolutions` stream: reason, mode, offered, accepted, mrr, intervention_type, arm. This
module turns that append-only log into what a customer sees -- a recent-events feed and rolled-up
churn intelligence -- WITHOUT any new instrumentation.

Two invariants matter here:

  - **Isolation is enforced at read time.** `read_stream` returns every tenant's rows (one shared
    log); every function here filters to a single `customer_id` FIRST and never returns a row that
    isn't the caller's. The endpoint layer authorizes that the caller owns that id (authorize_manage);
    this module makes sure it can only ever see that id's data.
  - **Aggregate-first / privacy.** Events expose outcome-level fields (reason, offer, accepted, mrr)
    but never the pseudonymized `user_id` -- the dashboard is about churn intelligence, not
    per-person surveillance, and the raw id never existed in the log anyway (see transcripts).

Reads hit disk per call (glob + parse the partitions). That's fine at tier-1 volume; a busy tenant
is the trigger to cache or move the sink to a DB (the `api.transcripts` seam already flagged for
metering), not to change this contract.
"""

from collections import Counter
from pathlib import Path
from typing import Union

from .runs_io import read_stream

# Fields from a resolution row that are safe to hand back to the customer as an "event". Deliberately
# omits user_id (pseudonymized, but aggregate-first) and arm/intended_* (internal holdout mechanics).
_EVENT_FIELDS = (
    "logged_at",
    "session_id",
    "reason",
    "mode",
    "intervention_type",
    "offered",
    "accepted",
    "mrr",
)


def _rows_for(directory: Union[str, Path], customer_id: str) -> list[dict]:
    """Every resolution row belonging to exactly this customer. The isolation boundary: callers in
    this module start here and never widen it."""
    return [
        r
        for r in read_stream(directory, "resolutions")
        if r.get("customer_id") == customer_id
    ]


def recent_events(directory: Union[str, Path], customer_id: str, limit: int = 50) -> list[dict]:
    """The tenant's most recent session outcomes, newest first, projected to customer-safe fields.
    `logged_at` is an ISO-8601 UTC string, so lexical sort is chronological."""
    rows = _rows_for(directory, customer_id)
    rows.sort(key=lambda r: r.get("logged_at") or "", reverse=True)
    return [{k: r.get(k) for k in _EVENT_FIELDS} for r in rows[: max(0, limit)]]


def _mrr(row: dict) -> float:
    """A row's MRR as a number, tolerating a missing/None field (control rows may not carry it)."""
    v = row.get("mrr")
    return float(v) if isinstance(v, (int, float)) else 0.0


def aggregate(directory: Union[str, Path], customer_id: str) -> dict:
    """Rolled-up churn intelligence for one tenant. The headline story the buyer can't see today:
    what the REAL reasons were, how many saves the engine won, and the MRR it retained.

    `save_rate` is the OBSERVATIONAL accept rate (accepted / offered) -- honest, but not causal; the
    holdout lift lives in the `learning` readout, surfaced in a later dashboard tier. `retained_mrr`
    sums the MRR of accepted offers (a gross figure, not yet net of the counterfactual)."""
    rows = _rows_for(directory, customer_id)
    offered = [r for r in rows if r.get("offered")]
    accepted = [r for r in offered if r.get("accepted")]

    # Reason distribution across ALL sessions (offered or not) -- this is the diagnosis story.
    reason_counts = Counter(r.get("reason") for r in rows if r.get("reason"))

    # Per-reason funnel: how often each real reason showed up, got an offer, and converted.
    by_reason: dict[str, dict] = {}
    for r in rows:
        reason = r.get("reason")
        if not reason:
            continue
        cell = by_reason.setdefault(
            reason, {"sessions": 0, "offered": 0, "accepted": 0, "retained_mrr": 0.0}
        )
        cell["sessions"] += 1
        if r.get("offered"):
            cell["offered"] += 1
            if r.get("accepted"):
                cell["accepted"] += 1
                cell["retained_mrr"] += _mrr(r)

    # Per-intervention-type funnel: which offers were served and which landed.
    by_type: dict[str, dict] = {}
    for r in offered:
        itype = r.get("intervention_type") or "unknown"
        cell = by_type.setdefault(itype, {"offered": 0, "accepted": 0, "retained_mrr": 0.0})
        cell["offered"] += 1
        if r.get("accepted"):
            cell["accepted"] += 1
            cell["retained_mrr"] += _mrr(r)

    return {
        "customer_id": customer_id,
        "total_sessions": len(rows),
        "offers_made": len(offered),
        "offers_accepted": len(accepted),
        "save_rate": (len(accepted) / len(offered)) if offered else None,
        "retained_mrr": round(sum(_mrr(r) for r in accepted), 2),
        "reason_breakdown": dict(reason_counts.most_common()),
        "by_reason": by_reason,
        "by_intervention_type": by_type,
    }
