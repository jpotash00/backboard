"""Full audit rig -- the honest, defensible version of the Milestone-1 eval.

`run_eval` scores ONE pass with the interviewer and the churners played by the SAME
model. That produces a point estimate that (a) has real run-to-run variance -- a couple
of personas flip between runs -- and (b) is open to the "same model on both sides is
colluding" objection. This rig closes both holes:

  VARIANCE  -- every persona is run N times. We report each persona's HIT-RATE and the
               SPREAD of per-trial accuracy (mean +/- std, min..max), not a lucky single
               number.
  COLLUSION -- the `cross` arm plays the churners with a DIFFERENT model than the
               interviewer. Same vendor (a true cross-vendor check needs a second API
               key we don't have), but a different checkpoint already breaks the
               same-model shared-prior objection. If the number survives, the score is
               about the interview, not about two copies of one model winking at
               each other.
  FLAKES    -- unparseable model output falls back to `unknown/0.0` and would silently
               score as a miss. Those are bucketed by the fallback's evidence string and
               reported separately, both counted as misses (conservative) and excluded
               (capability-only). The interviewer now retries once, so they should be rare.

Run:  python -m eval.audit                         (N=5, both arms)
      python -m eval.audit --trials 3 --arms same
      python -m eval.audit --cross-persona-model claude-haiku-4-5-20251001
"""

import argparse
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .personas import build_personas
from .run_eval import run_persona, to_result
from .scoring import COVER_TO_NAIVE_REASON, baseline_reason

# The distinctive evidence string the interviewer stamps on the parse-failure fallback
# (engine/interviewer.py). Reason "unknown" ALONE is a legitimate low-confidence diagnosis,
# so we key flakes off this string, never off the reason id.
FLAKE_EVIDENCE = "model returned unparseable output"

INTERVIEWER_MODEL = os.getenv("CHURN_MODEL", "claude-sonnet-5")
SAME_PERSONA_MODEL = os.getenv("PERSONA_MODEL", "claude-sonnet-5")
DEFAULT_CROSS_PERSONA_MODEL = "claude-opus-4-8"


def _is_trap(cover_story: str, hidden_reason: str) -> bool:
    """A dropdown that believed the cover story would get this one WRONG."""
    return COVER_TO_NAIVE_REASON.get(cover_story) != hidden_reason


def run_one(persona_id: int, arm: str, interviewer_model: str,
            persona_model: str, client) -> dict:
    """One blind interview. Builds a FRESH persona so roleplay state can't leak across
    concurrent trials, then flattens the outcome into a scored row."""
    persona = next(p for p in build_personas() if p.id == persona_id)
    t = run_persona(persona, client,
                    interviewer_model=interviewer_model, persona_model=persona_model)
    r = to_result(persona, t)
    o = t.outcome
    is_flake = bool(o is not None and o.evidence == FLAKE_EVIDENCE)
    return {
        "arm": arm,
        "persona_id": persona_id,
        "hidden_reason": persona.hidden_reason,
        "cover_story": persona.cover_story,
        "diagnosed_reason": r.diagnosed_reason,
        "confidence": r.confidence,
        "turns_used": r.turns_used,
        "reason_correct": r.reason_correct,
        "intervention_correct": r.intervention_correct,
        "is_trap": _is_trap(persona.cover_story, persona.hidden_reason),
        "is_flake": is_flake,
        "misleading_tell": persona.misleading_tell,
        "false_confirmer": persona.false_confirmer,
    }


def run_arm(arm: str, persona_model: str, trials: int, workers: int,
            client) -> list[dict]:
    """Every persona x every trial, fanned out over a thread pool (each interview is
    independent and I/O-bound, so threads give a big wall-clock win under the API)."""
    persona_ids = [p.id for p in build_personas()]
    tasks = [(pid, trial) for trial in range(trials) for pid in persona_ids]
    rows: list[dict] = []
    print(f"  arm={arm}: {len(tasks)} interviews "
          f"(interviewer={INTERVIEWER_MODEL}, personas={persona_model}) ...",
          file=sys.stderr)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_one, pid, arm, INTERVIEWER_MODEL, persona_model, client):
                (pid, trial)
            for pid, trial in tasks
        }
        done = 0
        for fut in as_completed(futures):
            pid, trial = futures[fut]
            try:
                row = fut.result()
                row["trial"] = trial
                rows.append(row)
            except Exception as e:  # a single interview blowing up shouldn't sink the arm
                print(f"    ! persona {pid} trial {trial} errored: {e}", file=sys.stderr)
            done += 1
            if done % 10 == 0 or done == len(tasks):
                print(f"    {done}/{len(tasks)} done", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _mean_std(xs: list[float]) -> tuple[float, float, float, float]:
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    m = statistics.mean(xs)
    sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return m, sd, min(xs), max(xs)


def per_trial_accuracy(rows: list[dict], trials: int, *, exclude_flakes: bool,
                       only=None) -> list[float]:
    """Accuracy computed WITHIN each trial (so the spread across trials is a real
    measure of run-to-run variance), optionally restricted to a subset (traps etc.)
    and optionally dropping flakes from the denominator."""
    out = []
    for trial in range(trials):
        sub = [r for r in rows if r["trial"] == trial]
        if only is not None:
            sub = [r for r in sub if only(r)]
        if exclude_flakes:
            sub = [r for r in sub if not r["is_flake"]]
        if sub:
            out.append(sum(r["reason_correct"] for r in sub) / len(sub))
    return out


def summarize_arm(arm: str, rows: list[dict], trials: int) -> dict:
    n_interviews = len(rows)
    n_flakes = sum(r["is_flake"] for r in rows)

    # Overall accuracy per trial, two ways: flakes-as-miss (conservative) and flakes-excluded.
    acc_incl = per_trial_accuracy(rows, trials, exclude_flakes=False)
    acc_excl = per_trial_accuracy(rows, trials, exclude_flakes=True)
    pen_incl = per_trial_accuracy(rows, trials, exclude_flakes=False, only=lambda r: r["is_trap"])
    pen_excl = per_trial_accuracy(rows, trials, exclude_flakes=True, only=lambda r: r["is_trap"])
    mis_excl = per_trial_accuracy(rows, trials, exclude_flakes=True, only=lambda r: r["misleading_tell"])
    fc_excl = per_trial_accuracy(rows, trials, exclude_flakes=True, only=lambda r: r["false_confirmer"])

    # Per-persona hit-rate across trials (flakes excluded from the denominator).
    by_persona: dict[int, list[dict]] = {}
    for r in rows:
        by_persona.setdefault(r["persona_id"], []).append(r)
    persona_rates = []
    for pid in sorted(by_persona):
        rs = by_persona[pid]
        real = [r for r in rs if not r["is_flake"]]
        hit = sum(r["reason_correct"] for r in real)
        persona_rates.append({
            "persona_id": pid,
            "hidden_reason": rs[0]["hidden_reason"],
            "cover_story": rs[0]["cover_story"],
            "is_trap": rs[0]["is_trap"],
            "misleading_tell": rs[0]["misleading_tell"],
            "false_confirmer": rs[0]["false_confirmer"],
            "hits": hit,
            "n": len(real),
            "flakes": sum(r["is_flake"] for r in rs),
            "rate": (hit / len(real)) if real else None,
        })

    # Calibration + danger count over the whole arm (flakes excluded -- a parse fallback
    # is 0.0 confidence by construction and would fake-inflate the "confident when right" gap).
    real_rows = [r for r in rows if not r["is_flake"]]
    conf_right = [r["confidence"] for r in real_rows if r["reason_correct"]]
    conf_wrong = [r["confidence"] for r in real_rows if not r["reason_correct"]]
    confident_wrong = sum(1 for r in real_rows
                          if not r["reason_correct"] and r["confidence"] >= 0.6)

    # The dropdown baseline is deterministic -> identical every trial, so no spread.
    base_all = sum(baseline_reason(r["cover_story"]) == r["hidden_reason"]
                   for r in rows) / n_interviews if n_interviews else 0.0
    traps = [r for r in rows if r["is_trap"]]
    base_traps = (sum(baseline_reason(r["cover_story"]) == r["hidden_reason"]
                      for r in traps) / len(traps)) if traps else 0.0

    return {
        "arm": arm,
        "persona_model": SAME_PERSONA_MODEL if arm == "same" else None,
        "n_interviews": n_interviews,
        "n_flakes": n_flakes,
        "accuracy_incl_flakes": _mean_std(acc_incl),
        "accuracy_excl_flakes": _mean_std(acc_excl),
        "penetration_incl_flakes": _mean_std(pen_incl),
        "penetration_excl_flakes": _mean_std(pen_excl),
        "misleading_tell_excl": _mean_std(mis_excl),
        "false_confirmer_excl": _mean_std(fc_excl),
        "baseline_accuracy": base_all,
        "baseline_penetration": base_traps,
        "mean_conf_right": statistics.mean(conf_right) if conf_right else 0.0,
        "mean_conf_wrong": statistics.mean(conf_wrong) if conf_wrong else 0.0,
        "confident_and_wrong": confident_wrong,
        "persona_rates": persona_rates,
    }


def _fmt(ms: tuple[float, float, float, float]) -> str:
    m, sd, lo, hi = ms
    return f"{m:5.0%} +/- {sd:4.1%}  [{lo:.0%}..{hi:.0%}]"


def print_summary(s: dict, trials: int) -> None:
    print("\n" + "#" * 82)
    print(f"#  AUDIT ARM: {s['arm'].upper()}   "
          f"(interviewer={INTERVIEWER_MODEL}, personas="
          f"{s['persona_model'] or '[cross-model]'})")
    print(f"#  {trials} trials/persona, {s['n_interviews']} interviews, "
          f"{s['n_flakes']} parse-flake(s)")
    print("#" * 82)

    print("\n  per-persona hit-rate (across trials, flakes excluded):")
    print(f"  {'#':>2}  {'true reason':<20} {'cover':<16} "
          f"{'trap':>4} {'tell':>5} {'lead':>5}  {'rate':>7}")
    for p in s["persona_rates"]:
        rate = "n/a" if p["rate"] is None else f"{p['hits']}/{p['n']}"
        flag = ""
        if p["rate"] is not None and p["rate"] < 0.6:
            flag = "  <-- weak"
        elif p["flakes"]:
            flag = f"  ({p['flakes']} flake)"
        print(f"  {p['persona_id']:>2}  {p['hidden_reason']:<20} {p['cover_story']:<16} "
              f"{'yes' if p['is_trap'] else ' - ':>4} "
              f"{'lies' if p['misleading_tell'] else ' - ':>5} "
              f"{'yes' if p['false_confirmer'] else ' - ':>5}  {rate:>7}{flag}")

    print(f"\n  {'metric':<40}{'interviewer (mean +/- sd)':>30}{'dropdown':>10}")
    print("  " + "-" * 78)
    print(f"  {'diagnostic accuracy (flakes=miss)':<40}{_fmt(s['accuracy_incl_flakes']):>30}"
          f"{s['baseline_accuracy']:>10.0%}")
    print(f"  {'diagnostic accuracy (flakes excluded)':<40}{_fmt(s['accuracy_excl_flakes']):>30}"
          f"{'':>10}")
    print(f"  {'cover-story penetration (traps)':<40}{_fmt(s['penetration_excl_flakes']):>30}"
          f"{s['baseline_penetration']:>10.0%}   <- THE number")
    if s["misleading_tell_excl"][0] or True:
        print(f"  {'misleading-tell accuracy':<40}{_fmt(s['misleading_tell_excl']):>30}"
              f"{'':>10}   <- anti-telegraphing")
    print(f"  {'false-confirmer accuracy':<40}{_fmt(s['false_confirmer_excl']):>30}"
          f"{'':>10}   <- anti-leading")
    print(f"\n  calibration: mean conf RIGHT={s['mean_conf_right']:.2f}  "
          f"WRONG={s['mean_conf_wrong']:.2f}  "
          f"(gap {s['mean_conf_right'] - s['mean_conf_wrong']:+.2f}, want positive)")
    print(f"  confident-and-wrong (conf>=0.6 but wrong): {s['confident_and_wrong']}  (want ~0)")


def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5, help="repeats per persona per arm")
    ap.add_argument("--arms", choices=["same", "cross", "both"], default="both")
    ap.add_argument("--workers", type=int, default=6, help="concurrent interviews")
    ap.add_argument("--cross-persona-model", default=DEFAULT_CROSS_PERSONA_MODEL)
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = anthropic.Anthropic()
    out_dir = Path("runs")
    out_dir.mkdir(exist_ok=True)

    arms = []
    if args.arms in ("same", "both"):
        arms.append(("same", SAME_PERSONA_MODEL))
    if args.arms in ("cross", "both"):
        arms.append(("cross", args.cross_persona_model))

    all_rows: list[dict] = []
    summaries = []
    for arm, persona_model in arms:
        rows = run_arm(arm, persona_model, args.trials, args.workers, client)
        all_rows.extend(rows)
        s = summarize_arm(arm, rows, args.trials)
        s["persona_model"] = persona_model
        summaries.append(s)
        print_summary(s, args.trials)

    path = out_dir / f"audit-{stamp}.jsonl"
    with path.open("w") as f:
        f.write(json.dumps({"kind": "meta", "trials": args.trials,
                            "interviewer_model": INTERVIEWER_MODEL,
                            "arms": [a for a, _ in arms]}) + "\n")
        for r in all_rows:
            f.write(json.dumps({"kind": "row", **r}) + "\n")
        for s in summaries:
            f.write(json.dumps({"kind": "summary", **s}) + "\n")

    if len(summaries) == 2:
        same, cross = summaries[0], summaries[1]
        drop = same["penetration_excl_flakes"][0] - cross["penetration_excl_flakes"][0]
        print("\n" + "=" * 82)
        print("  COLLUSION CHECK (penetration on traps):")
        print(f"    same-model : {_fmt(same['penetration_excl_flakes'])}")
        print(f"    cross-model: {_fmt(cross['penetration_excl_flakes'])}")
        print(f"    drop when personas play on a different model: {drop:+.0%}")
        print("=" * 82)

    print(f"\n  full rows + summaries logged to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
