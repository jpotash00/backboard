"""The causal readout: what the holdout was for.

learning.recalibrate estimates save_prior * effectiveness from accept rate (accepted /
offered). That number is OBSERVATIONAL and biased two ways its own docstring admits:
  - it counts ALWAYS-STAYERS (users who'd have retained with no offer) as saves, and
  - it can't see POST-ACCEPT CHURN (accepted the offer, cancelled next month anyway).

This module removes both by joining the arm-assigned resolution log to the downstream
retention outcomes (POST /outcomes) and comparing arms at a fixed horizon:

    lift(reason, intended_type) = R_treatment - R_control

where R is the fraction still subscribed at the horizon. That lift IS the causal
save_prior[reason] * effectiveness[type] the engine prices with -- measured, not guessed.
Always-stayers cancel out (they retain in both arms); post-accept churn is captured
(retention is measured after the horizon, not at the moment of accept).

Everything here is pure functions over the two logs plus a horizon. No model, no API.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .stats import newcombe_diff_ci, two_proportion_p, wilson_interval

CONTROL = "control"
TREATMENT = "treatment"


# --------------------------------------------------------------------------------------
# Load + join
# --------------------------------------------------------------------------------------
def _load_jsonl(path: str) -> list[dict]:
    """Read a run stream. `path` is the legacy monolithic file (e.g. runs/resolutions.jsonl);
    we resolve it to its directory + stem and read EVERY date partition too (runs_io.read_stream),
    so the readout spans all days whether the data is one old file, many partitions, or both."""
    from api.runs_io import read_stream, stem_of

    p = Path(path)
    return read_stream(p.parent, stem_of(p))


def _parse_ts(raw: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


@dataclass
class JoinedSession:
    """One session that has reached the horizon and can be scored. `retained` is the user's
    subscribed status at/after the horizon; `arm`/`reason`/`intended_type` place it in a cell."""
    arm: str
    reason: str
    intended_type: Optional[str]
    retained: bool
    offered: bool
    accepted: bool
    mrr: float


@dataclass
class JoinReport:
    joined: list[JoinedSession] = field(default_factory=list)
    pending: int = 0        # session reached, but no outcome observed at/after the horizon yet
    unmatched: int = 0      # resolution with no outcome report for that user at all
    skipped: int = 0        # malformed rows (missing reason/timestamp)


def join_sessions(
    resolutions: list[dict], outcomes: list[dict], horizon_days: int
) -> JoinReport:
    """Attach each resolution to the retention observation at/after its horizon.

    Retention is measured at a FIXED horizon: for a session decided at t0, we take the
    earliest outcome observed at or after t0 + horizon_days (the reading closest to the
    horizon). A user active before the horizon but cancelled by it counts as churned -- that
    is the point. Sessions with no mature observation are held out of the readout as
    `pending`/`unmatched`, never guessed."""
    horizon = timedelta(days=horizon_days)
    # (customer, user) -> sorted [(observed_at, active)]
    obs: dict[tuple[str, str], list[tuple[datetime, bool]]] = {}
    for o in outcomes:
        ts = _parse_ts(o.get("observed_at", ""))
        if ts is None:
            continue
        key = (o.get("customer_id"), o.get("user_id"))
        obs.setdefault(key, []).append((ts, bool(o.get("active"))))
    for v in obs.values():
        v.sort(key=lambda t: t[0])

    report = JoinReport()
    for r in resolutions:
        reason = r.get("reason")
        t0 = _parse_ts(r.get("logged_at", ""))
        if not reason or t0 is None:
            report.skipped += 1
            continue
        key = (r.get("customer_id"), r.get("user_id"))
        series = obs.get(key)
        if not series:
            report.unmatched += 1
            continue
        deadline = t0 + horizon
        mature = next((active for ts, active in series if ts >= deadline), None)
        if mature is None:
            report.pending += 1
            continue
        report.joined.append(JoinedSession(
            arm=r.get("arm", TREATMENT),
            reason=reason,
            intended_type=r.get("intended_intervention_type"),
            retained=bool(mature),
            offered=bool(r.get("offered")),
            accepted=bool(r.get("accepted")),
            mrr=float(r.get("mrr", 0.0) or 0.0),
        ))
    return report


# --------------------------------------------------------------------------------------
# Per-cell causal estimate
# --------------------------------------------------------------------------------------
@dataclass
class CellResult:
    reason: str
    intended_type: Optional[str]
    n_treatment: int
    retained_treatment: int
    n_control: int
    retained_control: int
    # Accept side, for contrast with the causal number (treatment offers only).
    n_offered: int
    n_accepted: int
    mean_mrr: float

    @property
    def r_treatment(self) -> Optional[float]:
        return self.retained_treatment / self.n_treatment if self.n_treatment else None

    @property
    def r_control(self) -> Optional[float]:
        return self.retained_control / self.n_control if self.n_control else None

    @property
    def accept_rate(self) -> Optional[float]:
        """The OBSERVATIONAL number recalibrate uses. Kept here to show the gap vs lift."""
        return self.n_accepted / self.n_offered if self.n_offered else None

    @property
    def lift(self) -> Optional[float]:
        """Causal incremental retention = R_treatment - R_control. This is the real
        save_prior * effectiveness. None until BOTH arms have data."""
        if self.r_treatment is None or self.r_control is None:
            return None
        return self.r_treatment - self.r_control

    def lift_ci(self) -> Optional[tuple[float, float]]:
        if self.n_treatment == 0 or self.n_control == 0:
            return None
        return newcombe_diff_ci(self.retained_treatment, self.n_treatment,
                                self.retained_control, self.n_control)

    def p_value(self) -> Optional[float]:
        if self.n_treatment == 0 or self.n_control == 0:
            return None
        return two_proportion_p(self.retained_treatment, self.n_treatment,
                                self.retained_control, self.n_control)

    @property
    def significant(self) -> bool:
        """Lift is significant at 95% when its confidence interval excludes 0."""
        ci = self.lift_ci()
        return ci is not None and (ci[0] > 0 or ci[1] < 0)

    def net_value(self, value_horizon_months: int, cost: float) -> Optional[float]:
        """Expected $ per treated user: (causal lift x LTV) minus margin actually spent.
        Margin is only spent on ACCEPTED offers, so it's weighted by accept rate. Negative
        means the offer gives away more than it saves -- the thing accept rate can't reveal."""
        if self.lift is None or self.accept_rate is None:
            return None
        ltv = self.mean_mrr * value_horizon_months
        return round(self.lift * ltv - cost * self.accept_rate, 2)


def cells(joined: list[JoinedSession]) -> list[CellResult]:
    """Aggregate joined sessions into (reason, intended_type) cells with both arms."""
    acc: dict[tuple[str, Optional[str]], dict] = {}
    for j in joined:
        key = (j.reason, j.intended_type)
        c = acc.setdefault(key, dict(nt=0, rt=0, nc=0, rc=0, no=0, na=0, mrr=0.0, m=0))
        if j.arm == CONTROL:
            c["nc"] += 1
            c["rc"] += 1 if j.retained else 0
        else:
            c["nt"] += 1
            c["rt"] += 1 if j.retained else 0
            if j.offered:
                c["no"] += 1
                c["na"] += 1 if j.accepted else 0
        c["mrr"] += j.mrr
        c["m"] += 1
    results = []
    for (reason, itype), c in acc.items():
        results.append(CellResult(
            reason=reason, intended_type=itype,
            n_treatment=c["nt"], retained_treatment=c["rt"],
            n_control=c["nc"], retained_control=c["rc"],
            n_offered=c["no"], n_accepted=c["na"],
            mean_mrr=round(c["mrr"] / c["m"], 2) if c["m"] else 0.0,
        ))
    results.sort(key=lambda r: (r.reason, r.intended_type or ""))
    return results


def analyze(
    resolutions_path: str = "runs/resolutions.jsonl",
    outcomes_path: str = "runs/outcomes.jsonl",
    horizon_days: int = 30,
) -> tuple[list[CellResult], JoinReport]:
    joined = join_sessions(
        _load_jsonl(resolutions_path), _load_jsonl(outcomes_path), horizon_days
    )
    return cells(joined.joined), joined


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------
def report(results: list[CellResult], join: JoinReport, min_per_arm: int = 30) -> str:
    lines = ["CAUSAL LIFT READOUT (holdout vs treatment)", "=" * 60]
    lines.append(
        f"  joined={len(join.joined)}  pending={join.pending}  "
        f"unmatched={join.unmatched}  skipped={join.skipped}"
    )
    lines.append("")
    header = (f"  {'reason':<20}{'offer':<12}{'R_ctl':>7}{'R_trt':>7}"
              f"{'lift':>8}{'95% CI':>18}{'accept':>8}  note")
    lines.append(header)
    for c in results:
        r0 = "-" if c.r_control is None else f"{c.r_control:.2f}"
        r1 = "-" if c.r_treatment is None else f"{c.r_treatment:.2f}"
        lift = "-" if c.lift is None else f"{c.lift:+.2f}"
        ci = c.lift_ci()
        ci_s = "-" if ci is None else f"[{ci[0]:+.2f},{ci[1]:+.2f}]"
        acc = "-" if c.accept_rate is None else f"{c.accept_rate:.2f}"
        underpowered = c.n_treatment < min_per_arm or c.n_control < min_per_arm
        if c.n_control == 0:
            note = "NO CONTROL (raise holdout_fraction)"
        elif underpowered:
            note = f"underpowered (nt={c.n_treatment}, nc={c.n_control})"
        elif c.significant:
            note = "significant"
        else:
            note = "not significant (CI spans 0)"
        lines.append(
            f"  {c.reason:<20}{(c.intended_type or '-'):<12}{r0:>7}{r1:>7}"
            f"{lift:>8}{ci_s:>18}{acc:>8}  {note}"
        )
    lines.append("")
    lines.append("  lift = R_treatment - R_control = the CAUSAL save_prior x effectiveness.")
    lines.append("  Where 'accept' >> 'lift', accept rate was counting always-stayers as saves.")
    return "\n".join(lines)


def main() -> int:
    import sys

    res = sys.argv[1] if len(sys.argv) > 1 else "runs/resolutions.jsonl"
    out = sys.argv[2] if len(sys.argv) > 2 else "runs/outcomes.jsonl"
    horizon = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    results, join = analyze(res, out, horizon)
    if not join.joined:
        print(f"No mature joined sessions yet (pending={join.pending}, "
              f"unmatched={join.unmatched}). Need outcomes past the {horizon}d horizon.")
        return 0
    print(report(results, join))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
