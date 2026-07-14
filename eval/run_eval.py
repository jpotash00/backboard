"""Milestone 1 -- the blind eval (§5). BUILD AND RUN THIS FIRST.

For each of the ten synthetic churners:
  interviewer.open() -> persona replies with the cover story -> interviewer.turn()
  ... up to MAX_TURNS ... -> Outcome -> policy.decide() -> authorized intervention.

The interviewer sees only the cover story + behavioral data. It never sees the
persona's hidden reason. Then we score, print full transcripts, and compare against
the dropdown baseline. The delta is the product.

Run:  python -m eval.run_eval           (needs ANTHROPIC_API_KEY)
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic

from engine import Interviewer, Outcome, decide
from engine.interviewer import MAX_TURNS
from .configs import ACME
from .personas import Persona, build_personas
from .scoring import Result, baseline_reason, crux_split_ok, score
from .snapshot_only import snapshot_only_reason


@dataclass
class Turn:
    speaker: str   # "interviewer" | "churner"
    text: str


@dataclass
class Transcript:
    persona_id: int
    hidden_reason: str
    turns: list[Turn] = field(default_factory=list)
    outcome: Optional[Outcome] = None


def run_persona(persona: Persona, client, interviewer_model=None,
                persona_model=None) -> Transcript:
    """Play one full interview, blind. Returns the transcript with the final Outcome.

    interviewer_model / persona_model let the audit rig drive the two sides with
    different models (the cross-model collusion check); None keeps the defaults."""
    interviewer = Interviewer(ACME, persona.user, client=client, model=interviewer_model)
    t = Transcript(persona_id=persona.id, hidden_reason=persona.hidden_reason)

    question = interviewer.open()
    t.turns.append(Turn("interviewer", question))

    outcome: Optional[Outcome] = None
    # open() doesn't count against MAX_TURNS; the loop drives the ask/answer rounds.
    while outcome is None:
        if question is None:
            break
        reply = persona.respond(question, ACME, client=client, model=persona_model)
        t.turns.append(Turn("churner", reply))
        question, outcome = interviewer.turn(reply)
        if question is not None:
            t.turns.append(Turn("interviewer", question))

    if outcome is not None:
        t.outcome = decide(outcome, ACME, persona.user)
    return t


def to_result(persona: Persona, transcript: Transcript,
              snapshot_only: Optional[str] = None) -> Result:
    o = transcript.outcome
    if o is None:
        raise ValueError(f"persona {persona.id}: interview ended without an outcome")
    iv_type = next(
        (i.type for i in ACME.interventions if i.id == o.intervention_id), None
    )
    return Result(
        persona_id=persona.id,
        hidden_reason=persona.hidden_reason,
        cover_story=persona.cover_story,
        diagnosed_reason=o.reason,
        confidence=o.confidence,
        turns_used=o.turns_used,
        intervention_type=iv_type,
        expected_intervention_type=persona.expected_intervention_type,
        misleading_tell=persona.misleading_tell,
        false_confirmer=persona.false_confirmer,
        snapshot_only_reason=snapshot_only,
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def print_transcript(persona: Persona, t: Transcript, r: Result) -> None:
    o = t.outcome
    if o is None:
        raise ValueError(f"persona {persona.id}: interview ended without an outcome")
    hit = "✅" if r.reason_correct else "❌"
    print("\n" + "=" * 78)
    print(f"PERSONA {persona.id}  |  hidden reason: {persona.hidden_reason}  |  "
          f"cover story: {persona.cover_story}")
    print(f"behavioral tell: {persona.user.usage_summary}")
    print("-" * 78)
    for turn in t.turns:
        who = "  interviewer" if turn.speaker == "interviewer" else "  churner    "
        print(f"{who}: {turn.text}")
    print("-" * 78)
    print(f"  DIAGNOSIS {hit}  reason={o.reason}  (true={persona.hidden_reason})  "
          f"conf={o.confidence:.2f}  turns={o.turns_used}")
    print(f"  cover_story seen: {o.cover_story}   savable: {o.savable}")
    print(f"  evidence: {o.evidence}")
    print(f"  intervention: {o.intervention_id or 'NONE'} "
          f"({r.intervention_type or 'none'})  "
          f"[expected: {persona.expected_intervention_type}] "
          f"{'✓' if r.intervention_correct else '✗'}")
    print(f"  rationale: {o.rationale}")


def print_summary(results: list[Result]) -> None:
    floor = ACME.policy.confidence_floor
    s = score(results, floor)
    crux = crux_split_ok(results)

    print("\n" + "#" * 78)
    print("#  MILESTONE 1 SCORES")
    print("#" * 78)

    print("\n  per persona:  (trap = dropdown wrong; tell = tell MISLEADS; lead = punishes leading Qs)")
    print(f"  {'#':>2}  {'true reason':<21} {'diagnosed':<21} {'conf':>5} "
          f"{'trap':>4} {'tell':>5} {'lead':>5} {'ok':>3}")
    for r in sorted(results, key=lambda r: r.persona_id):
        print(f"  {r.persona_id:>2}  {r.hidden_reason:<21} {r.diagnosed_reason:<21} "
              f"{r.confidence:>5.2f} {'yes' if r.is_cover_story_trap else ' - ':>4} "
              f"{'lies' if r.misleading_tell else ' - ':>5} "
              f"{'yes' if r.false_confirmer else ' - ':>5} "
              f"{'✅' if r.reason_correct else '❌':>3}")

    # Two baselines bracket the interview: `dropdown` believes the COVER story, `snapshot`
    # believes the DASHBOARD. The interview only earns its keep by beating BOTH.
    def _snap(v: Optional[float]) -> str:
        return f"{v:>10.0%}" if v is not None else f"{'  n/a':>10}"

    print(f"\n  {'metric':<34}{'interviewer':>13}{'dropdown':>10}{'snapshot':>11}")
    print("  " + "-" * 68)
    print(f"  {'diagnostic accuracy':<34}"
          f"{s.diagnostic_accuracy:>12.0%} {s.baseline_diagnostic_accuracy:>9.0%} "
          f"{_snap(s.snapshot_only_diagnostic_accuracy)}")
    print(f"  {'cover-story penetration (traps)':<34}"
          f"{s.cover_story_penetration:>12.0%} {s.baseline_cover_story_penetration:>9.0%} "
          f"{_snap(s.snapshot_only_cover_story_penetration)}   <- the real metric")
    print(f"  {'intervention correctness':<34}"
          f"{s.intervention_correctness:>12.0%}{'':>21}")
    if s.n_misleading:
        print(f"  {'misleading-tell accuracy':<34}"
              f"{s.misleading_tell_accuracy:>12.0%}{'':>10} "
              f"{_snap(s.snapshot_only_misleading_tell_accuracy)}"
              f"   <- n={s.n_misleading}; anti-telegraphing")
    if s.n_false_confirmer:
        print(f"  {'false-confirmer accuracy':<34}"
              f"{s.false_confirmer_accuracy:>13.0%}{'':>12}"
              f"   <- n={s.n_false_confirmer}; anti-leading (open Qs vs menus)")

    print(f"\n  calibration:  mean conf when RIGHT = {s.mean_conf_correct:.2f}   "
          f"when WRONG = {s.mean_conf_wrong:.2f}   "
          f"(gap {s.mean_conf_correct - s.mean_conf_wrong:+.2f}, want positive)")
    print(f"                confident-and-wrong (conf >= {floor}): "
          f"{s.confident_and_wrong}  (want 0 -- these are the dangerous ones)")
    print(f"  mean turns used: {s.mean_turns:.1f} / {MAX_TURNS}")

    # What the conversation adds over just reading the dashboard. If this gap is ~0, the
    # interview is decorative and the label leaked from the shown features (see snapshot_only).
    if s.snapshot_only_cover_story_penetration is not None:
        gap = s.cover_story_penetration - s.snapshot_only_cover_story_penetration
        print(f"\n  conversation lift over snapshot-only (on traps): {gap:+.0%}   "
              f"(interview {s.cover_story_penetration:.0%} vs dashboard-truster "
              f"{s.snapshot_only_cover_story_penetration:.0%})")
        if s.snapshot_only_cover_story_penetration >= 0.90:
            print("  ⚠ FEATURE LEAKAGE: a no-interview classifier already solves these from the "
                  "snapshot.\n    Penetration here is NOT measuring the interview -- redact the "
                  "discriminating\n    signals so they can only surface through questioning.")

    print("\n  " + "-" * 58)
    crux_str = {True: "✅ SPLIT", False: "❌ NOT SPLIT", None: "n/a"}[crux]
    print(f"  CRUX (persona 1 vs 2, same cover story, opposite truth): {crux_str}")
    pen_ok = s.cover_story_penetration >= 0.70
    print(f"  BAR: cover-story penetration >= 70%  ->  "
          f"{'✅ PASS' if pen_ok else '❌ FAIL'} ({s.cover_story_penetration:.0%})")
    verdict = pen_ok and crux is True
    print("\n  " + ("=" * 58))
    print(f"  MILESTONE 1: {'✅ PASSES -- proceed to Milestone 2' if verdict else '❌ DOES NOT PASS -- iterate the interviewer prompt before building anything else'}")
    print("  " + ("=" * 58))
    print("\n  Now read the transcripts above. Where it got stuck is the product spec.")


def log_run(results: list[Result], transcripts: list[Transcript], stamp: str) -> Path:
    """Structured JSONL of every transcript + outcome -- the data asset that compounds."""
    out_dir = Path("runs")
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"eval-{stamp}.jsonl"
    by_id = {t.persona_id: t for t in transcripts}
    with path.open("w") as f:
        for r in sorted(results, key=lambda r: r.persona_id):
            t = by_id[r.persona_id]
            if t.outcome is None:
                raise ValueError(f"persona {r.persona_id}: interview ended without an outcome")
            f.write(json.dumps({
                "persona_id": r.persona_id,
                "hidden_reason": r.hidden_reason,
                "cover_story": r.cover_story,
                "transcript": [{"speaker": x.speaker, "text": x.text} for x in t.turns],
                "diagnosed_reason": r.diagnosed_reason,
                "confidence": r.confidence,
                "turns_used": r.turns_used,
                "intervention_id": t.outcome.intervention_id,
                "intervention_type": r.intervention_type,
                "expected_intervention_type": r.expected_intervention_type,
                "reason_correct": r.reason_correct,
                "intervention_correct": r.intervention_correct,
                "baseline_reason": baseline_reason(r.cover_story),
                "snapshot_only_reason": r.snapshot_only_reason,
                "snapshot_only_correct": r.snapshot_only_correct,
            }) + "\n")
    return path


def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in, "
              "or export it, then re-run.", file=sys.stderr)
        return 1

    # Timestamp is passed in explicitly (not read mid-run) so runs are reproducible/named.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = anthropic.Anthropic()

    print(f"Running Milestone 1 eval  |  interviewer={os.getenv('CHURN_MODEL', 'claude-sonnet-5')}"
          f"  personas={os.getenv('PERSONA_MODEL', 'claude-sonnet-5')}")

    # Persona source. `authored` (default) is the curated 10-persona baseline; `ravenstack` draws
    # data-grounded personas from the RavenStack dataset (realistic context, reason derived from
    # behavior). Toggle with EVAL_SOURCE=ravenstack [EVAL_N=30] [EVAL_SEED=0].
    source = os.getenv("EVAL_SOURCE", "authored")
    if source == "ravenstack":
        from .ravenstack import build_ravenstack_personas
        personas = build_ravenstack_personas(n=int(os.getenv("EVAL_N", "30")),
                                             seed=int(os.getenv("EVAL_SEED", "0")))
        print(f"  source=ravenstack  |  {len(personas)} data-grounded personas")
    else:
        personas = build_personas()
    # The believe-the-dashboard adversary (§ snapshot_only): one extra call per persona,
    # no interview. It brackets the interview from the other side -- if it matches the
    # interviewer, the conversation added nothing. On by default; SNAPSHOT_ONLY=0 skips it.
    run_snapshot_only = os.getenv("SNAPSHOT_ONLY", "1") != "0"

    results: list[Result] = []
    transcripts: list[Transcript] = []
    for persona in personas:
        print(f"  ... interviewing persona {persona.id} ({persona.hidden_reason})",
              file=sys.stderr)
        t = run_persona(persona, client)
        transcripts.append(t)
        snap = None
        if run_snapshot_only:
            snap, _ = snapshot_only_reason(persona, ACME, client=client)
        r = to_result(persona, t, snapshot_only=snap)
        results.append(r)
        print_transcript(persona, t, r)

    print_summary(results)
    path = log_run(results, transcripts, stamp)
    print(f"\n  transcripts + outcomes logged to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
