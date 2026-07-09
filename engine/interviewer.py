"""The interviewer. Free path, fixed destination."""

import json
import os
from dataclasses import asdict
from typing import Optional

import anthropic
from anthropic.types import MessageParam

from .taxonomy import Outcome, ProductConfig, UserContext

MODEL = os.getenv("CHURN_MODEL", "claude-sonnet-5")
MAX_TURNS = 3  # hard ceiling. Raise it and watch completion rate fall.


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

Ask ONE question at a time. You get at most {max_turns} questions. Be warm, brief,
and human -- never corporate, never guilt-tripping, never try to talk them out of leaving.
You are trying to understand, not to save. Earn the answer.

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
