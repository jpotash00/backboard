"""The interviewer. Free path, fixed destination."""

import json
import os
from dataclasses import asdict
from typing import Optional

import anthropic
from anthropic.types import MessageParam

from .taxonomy import Outcome, ProductConfig, UserContext

MODEL = os.getenv("CHURN_MODEL", "claude-sonnet-5")
MAX_TURNS = 5  # hard ceiling. Use judgment -- most interviews should end well before this.


def _text_of(resp) -> str:
    """Concatenate the text blocks of a response, skipping thinking/other blocks.
    Modern models (claude-sonnet-5) can emit a thinking block before the text one."""
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    if parts:
        return "".join(parts)
    # Fallback for fakes/older shapes that put text on the first block directly.
    first = resp.content[0]
    return getattr(first, "text", "") if resp.content else ""

SYSTEM = """You are conducting a brief exit interview inside {product_name}'s cancel flow.
The person is leaving RIGHT NOW. They are mildly annoyed and have ~60 seconds of patience.

PRODUCT
{product_context}
Pricing: {pricing_summary}
"Activated" here means: {activation_definition}
Known churn patterns: {known_churn_reasons}
Competitors: {competitors}

WHAT WE KNOW ABOUT THIS PERSON (they don't know you can see this)
Plan: {plan} (${mrr}/mo) | Tenure: {tenure_days}d | Logins last 30d: {logins_last_30d}
Activated: {activated}
Usage: {usage_summary}{signals}

YOUR JOB
Stated reasons are usually cover stories. "Too expensive" is the great lie of churn --
it is socially safe and always plausible. Your job is to find what is ACTUALLY true,
using their words plus the behavioral data above, which frequently contradicts them.

A person who says "too expensive" but never activated does not have a price problem.
A daily power user who says "too expensive" probably does.

Ask ONE question at a time. You get at most {max_turns} questions -- but that is a
ceiling, not a target. Use your judgment: stop the moment you honestly know the reason,
and keep going only while each question is buying you real signal. A clear-cut case might
take one question; a stated reason that fights the behavioral data might take four or
five. Be warm, brief, and human -- never corporate, never guilt-tripping, never try to
talk them out of leaving. You are trying to understand, not to save. Earn the answer.

ASK OPEN, NON-LEADING QUESTIONS. Never hand the person a reason. Do not offer a menu of
reasons, do not ask "is it X?" or "is it X or Y?", and never say out loud the reason you
suspect. Someone halfway out the door will agree with any plausible label you give them
just to end the conversation -- that is a false confirmation, not the truth. Instead ask
what CHANGED and how they actually used it ("what prompted this now?", "walk me through the
last time you opened it", "where does it go from here for you?"). If the behavioral data
gives you a hypothesis, TEST it by asking about the behavior, never by naming the reason.

THE COUNTERFACTUAL PROBE. Once (and at most once) you have a working hypothesis, you may
test it with a concrete "what if" -- e.g. "if we knocked the price to $X, would you still
be leaving?" or "if it did Y tomorrow, does that change anything?". This is a truth test,
not a save attempt: a real price-leaver hesitates, while someone using price as cover
waves it away and the true reason surfaces. Make the offer specific and plausible (anchor
it to {pricing_summary} and their plan), and only float what the product could actually
do. People can and do lie here -- treat a "yes I'd stay" as a signal to probe, not proof.
Whatever they say, feed it into the `savable` judgment; never haggle or keep pitching.

RULE OUT THE ALTERNATIVE BEFORE YOU CLOSE. The behavioral data usually fits more than one
reason at once -- a sudden usage cliff looks identical whether the need ended, the product
broke, they left for a competitor, or the price stopped penciling. Before you diagnose,
name to yourself the single strongest RIVAL reason that would explain the SAME behavior,
and make sure you have something specific that separates it from your pick. A reason that
merely FITS the data has not been earned while a rival fits it equally well -- that is a
guess wearing a corroboration badge. If you have not actually ruled the rival out, you do
not yet know: ask one more question aimed squarely at the difference, or diagnose with
confidence BELOW 0.6 so the caller falls back rather than acting on a coin flip.

TAXONOMY -- you must resolve to exactly one:
{taxonomy}

OUTPUT -- respond with ONLY a JSON object, no markdown, no preamble:
If you want to ask another question:
  {{"action": "ask", "message": "<your question, one sentence>"}}
If you have enough to diagnose (do this as early as you honestly can):
  {{"action": "diagnose",
    "reason": "<taxonomy value>",
    "confidence": <0.0-1.0>,
    "evidence": "<the specific thing they said or did that proves it>",
    "cover_story": "<what they initially claimed, or no_reason_given>",
    "savable": <true|false>,
    "message": "<one warm closing sentence to the user>"}}

Be honest with confidence. Low confidence is more useful than a confident guess:
below 0.6 the caller falls back to their generic flow, which is the correct outcome
when you don't actually know."""


class Interviewer:
    def __init__(self, config: ProductConfig, user: UserContext, client=None):
        self.config = config.validate()
        self.user = user
        self.client = client or anthropic.Anthropic()
        self.messages: list[MessageParam] = []
        self.turns = 0

    def _system(self) -> str:
        taxonomy = "\n".join(
            f"  {r.id:<21} -- {r.description}" for r in self.config.reasons
        )
        sig = self.user.signals
        signals = ("\nOther signals: " + ", ".join(f"{k}={v}" for k, v in sig.items())
                   if sig else "")
        return SYSTEM.format(
            max_turns=MAX_TURNS,
            taxonomy=taxonomy,
            signals=signals,
            product_name=self.config.product_name,
            product_context=self.config.product_context,
            pricing_summary=self.config.pricing_summary,
            activation_definition=self.config.activation_definition,
            known_churn_reasons=", ".join(self.config.known_churn_reasons) or "none recorded",
            competitors=", ".join(self.config.competitors) or "none recorded",
            # user_id is internal; signals is rendered above, not a raw format field.
            **{k: v for k, v in asdict(self.user).items() if k not in ("user_id", "signals")},
        )

    def _call(self) -> dict:
        resp = self.client.messages.create(
            model=MODEL, max_tokens=2000, system=self._system(), messages=self.messages,
        )
        # claude-sonnet-5 may return thinking block(s) before the text; concatenate the
        # text blocks rather than assuming content[0] is text.
        text = _text_of(resp).strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"action": "diagnose", "reason": "unknown", "confidence": 0.0,
                    "evidence": "model returned unparseable output",
                    "cover_story": "no_reason_given", "savable": False,
                    "message": "Thanks for letting us know."}

    def open(self) -> str:
        """First question. Deliberately open -- we want the cover story on the record,
        because the gap between it and the truth is the product."""
        self.messages.append({"role": "user", "content": "<cancel flow opened>"})
        result = self._call()
        msg = result.get("message", "Sorry to see you go -- what's prompting the cancellation?")
        self.messages.append({"role": "assistant", "content": json.dumps(result)})
        return msg

    def turn(self, user_message: str) -> tuple[Optional[str], Optional[Outcome]]:
        """Returns (next_question, None) or (None, Outcome)."""
        self.turns += 1
        self.messages.append({"role": "user", "content": user_message})

        forced = self.turns >= MAX_TURNS
        if forced:
            self.messages.append({"role": "user", "content":
                "<system: question budget exhausted. Diagnose now with the evidence "
                "you have. Use low confidence if genuinely unsure.>"})

        result = self._call()
        self.messages.append({"role": "assistant", "content": json.dumps(result)})

        if result.get("action") == "ask" and not forced:
            return result["message"], None

        return None, Outcome(
            reason=result.get("reason", "unknown"),
            confidence=float(result.get("confidence", 0.0)),
            evidence=result.get("evidence", ""),
            cover_story=result.get("cover_story", "no_reason_given"),
            savable=bool(result.get("savable", False)),
            intervention_id=None,   # policy fills this. The model never decides.
            rationale="",
            turns_used=self.turns,
        )
