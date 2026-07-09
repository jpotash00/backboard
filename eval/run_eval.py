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


def run_persona(persona: Persona, client) -> Transcript:
    """Play one full interview, blind. Returns the transcript with the final Outcome."""
    interviewer = Interviewer(ACME, persona.user, client=client)
    t = Transcript(persona_id=persona.id, hidden_reason=persona.hidden_reason)

    question = interviewer.open()
    t.turns.append(Turn("interviewer", question))

    outcome: Optional[Outcome] = None
    # open() doesn't count against MAX_TURNS; the loop drives the ask/answer rounds.
    while outcome is None:
        if question is None:
            break
        reply = persona.respond(question, ACME, client=client)
        t.turns.append(Turn("churner", reply))
        question, outcome = interviewer.turn(reply)
        if question is not None:
            t.turns.append(Turn("interviewer", question))

    if outcome is not None:
        t.outcome = decide(outcome, ACME, persona.user)
    return t


def to_result(persona: Persona, transcript: Transcript) -> Result:
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

    print(f"\n  {'metric':<34}{'interviewer':>14}{'dropdown':>12}")
    print("  " + "-" * 58)
    print(f"  {'diagnostic accuracy':<34}"
          f"{s.diagnostic_accuracy:>13.0%} {s.baseline_diagnostic_accuracy:>11.0%}")
    print(f"  {'cover-story penetration (traps)':<34}"
          f"{s.cover_story_penetration:>13.0%} {s.baseline_cover_story_penetration:>11.0%}"
          f"   <- the real metric")
    print(f"  {'intervention correctness':<34}"
          f"{s.intervention_correctness:>13.0%}{'':>12}")
    if s.n_misleading:
        print(f"  {'misleading-tell accuracy':<34}"
              f"{s.misleading_tell_accuracy:>13.0%}{'':>12}"
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

    personas = build_personas()
    results: list[Result] = []
    transcripts: list[Transcript] = []
    for persona in personas:
        print(f"  ... interviewing persona {persona.id} ({persona.hidden_reason})",
              file=sys.stderr)
        t = run_persona(persona, client)
        transcripts.append(t)
        r = to_result(persona, t)
        results.append(r)
        print_transcript(persona, t, r)

    print_summary(results)
    path = log_run(results, transcripts, stamp)
    print(f"\n  transcripts + outcomes logged to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
