"""The snapshot-only adversary: the second baseline that brackets the interview.

`scoring.baseline_reason` is the *believe-the-cover-story* control (a dropdown that
trusts the opener). This is its opposite twin: the *believe-the-dashboard* control -- a
strong classifier handed the EXACT behavioral snapshot the interviewer sees, plus the
person's opening line, and asked to diagnose in one shot WITHOUT conducting the interview.

Why it exists. If the interviewer's score can be matched by a model that never asks a
question, the conversation isn't doing the work -- the label was recoverable from the
features on screen (feature leakage). On the RavenStack arm, where the hidden reason is a
deterministic function of the shown signals, this adversary scores ~100% and exposes the
leak automatically, forever. On the authored adversarial arm -- where several personas
share one misleading snapshot with different truths -- it should crater, which is the
measured proof that the interview, not the dashboard, is what recovers the truth.

The delta the eval actually cares about:  interviewer  -  snapshot-only  =  what the
conversation adds over just reading the dashboard. One extra API call per persona.
"""

import json
import os
from typing import Optional

import anthropic

from engine import Interviewer, ProductConfig
from engine.interviewer import _text_of
from .personas import Persona

# Fair, strong classifier. Same taxonomy + product context the interviewer gets, same
# snapshot, plus the opener -- everything except the ability to ask a single question.
SNAPSHOT_ONLY_SYSTEM = """You are a churn classifier for {product_name}. A customer is
cancelling. You are handed (a) the behavioral snapshot we have on this person and (b) the
one line they led with in the cancel flow. You do NOT get to ask them anything -- classify
from what is in front of you.

PRODUCT
{product_context}
Pricing: {pricing_summary}
"Activated" here means: {activation_definition}

The opener is often a socially-safe cover story ("too expensive" is the great lie of churn),
so weigh the behavioral snapshot heavily against it. Pick the single reason the data best
supports.

TAXONOMY -- resolve to exactly one:
{taxonomy}

Respond with ONLY a JSON object, no markdown:
{{"reason": "<taxonomy value>", "confidence": <0.0-1.0>}}"""


def _system(config: ProductConfig) -> str:
    taxonomy = "\n".join(f"  {r.id:<21} -- {r.description}" for r in config.reasons)
    return SNAPSHOT_ONLY_SYSTEM.format(
        product_name=config.product_name,
        product_context=config.product_context,
        pricing_summary=config.pricing_summary,
        activation_definition=config.activation_definition,
        taxonomy=taxonomy,
    )


def snapshot_only_reason(persona: Persona, config: ProductConfig, client=None,
                         model=None) -> tuple[str, float]:
    """Diagnose from the dashboard alone. Returns (reason, confidence).

    Uses the interviewer's OWN `_user_block` rendering so the snapshot is byte-identical
    to what the interviewer starts with -- the comparison is only fair if the inputs match.
    The opener (persona.opening_line) is appended as the customer's single stated reason."""
    client = client or anthropic.Anthropic()
    # Reuse the interviewer's snapshot rendering verbatim (no API call -- just string build).
    snapshot = Interviewer(config, persona.user, client=client)._user_block()
    user = f"{snapshot}\n\nTHEY LED WITH: \"{persona.opening_line}\""

    for _ in range(2):
        resp = client.messages.create(
            model=model or os.getenv("CHURN_MODEL", "claude-sonnet-5"),
            max_tokens=1000,
            system=_system(config),
            messages=[{"role": "user", "content": user}],
        )
        text = _text_of(resp).strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            d = json.loads(text)
            return str(d.get("reason", "unknown")), float(d.get("confidence", 0.0))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return "unknown", 0.0
