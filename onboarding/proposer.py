"""Propose a ProductConfig from messy inputs.

The safety model mirrors the rest of the system: the LLM only *structures* what it's given
(extract product facts; classify the customer's authorized offers into typed interventions).
The taxonomy and the whole policy rulebook come from **validated defaults**, so the
proposal is sound by construction -- and `config.validate()` is the backstop before a human
ever sees it. The LLM never invents policy; it fills a form.

What can't be inferred is the **offer menu** -- what saves the company is authorized to
make. That's a business decision, so it's a required input, not a guess.
"""

import json
import os
from dataclasses import dataclass, field

import anthropic

from engine import (
    DEFAULT_REASONS,
    Intervention,
    ProductConfig,
)
from engine.interviewer import _text_of

ONBOARD_MODEL = os.getenv("ONBOARD_MODEL", "claude-sonnet-5")

# The intervention types policy knows how to route. The LLM must classify each offer into
# one of these (it's an open field on Intervention, but routing only works for known types).
KNOWN_TYPES = ["discount", "downgrade", "pause", "onboarding", "roadmap", "support",
               "gift", "extend_trial"]


class ProposalError(Exception):
    """The model's proposal could not be turned into a valid ProductConfig."""


@dataclass
class ProposalInput:
    # The company's authorized save offers, free-form. REQUIRED -- a business decision, not
    # something to infer. e.g. ["50% off for 3 months", "free 15-min setup call", "pause up to 3 months"]
    offers: list[str]
    # Pasted pricing page / docs / help-center text. Optional, but the richer it is, the
    # better the extracted facts.
    product_text: str = ""
    # Optional hint if the text doesn't name the product.
    product_name: str = ""


@dataclass
class Proposal:
    config: ProductConfig          # validated, ready to review/approve
    notes: str                     # the model's assumptions the human should check
    raw: dict = field(default_factory=dict)   # the structured model output, for transparency


_SYSTEM = """You help onboard a company onto Offboard, a tool that runs a short exit
interview inside their cancel flow and then offers a save. Given a description of the
company's product and the list of save offers they have authorized, produce a STRUCTURED
config proposal a human will review.

Your job is extraction and classification, not invention:
- Pull the product facts out of the provided text. If something isn't stated, make a brief,
  clearly-reasonable assumption and record it in "notes" so the human can correct it.
- Classify EACH authorized offer into exactly one intervention `type` from this set:
  {types}. Write a short, display-ready `description` for each (what the user would see).
- Optionally add an `eligible_when` rule (a simple "field OP value" expression, joined by
  AND/OR) using any of: reason, tenure, mrr, activated, logins. Valid reason ids:
  {reason_ids}. Only add a rule when the offer clearly warrants gating (e.g. a discount
  for longer-tenured price-sensitive users: "reason == price_value_mismatch AND tenure > 90").

Output ONLY a JSON object, no markdown, matching:
{{
  "product_name": "...",
  "product_context": "one or two sentences on what the product does and for whom",
  "activation_definition": "the concrete first-value moment (what 'activated' means here)",
  "pricing_summary": "the plans and prices, one line",
  "competitors": ["..."],
  "known_churn_reasons": ["short phrases, if evident from the text"],
  "interventions": [
    {{"id": "snake_case_id", "type": "<one of the set>", "description": "...", "eligible_when": null}}
  ],
  "notes": "assumptions you made and anything the human should verify"
}}"""


def propose_config(inp: ProposalInput, client=None) -> Proposal:
    if not inp.offers:
        raise ProposalError("at least one authorized offer is required (offers can't be inferred)")
    client = client or anthropic.Anthropic()

    reason_ids = [r.id for r in DEFAULT_REASONS]
    system = _SYSTEM.format(types=", ".join(KNOWN_TYPES), reason_ids=", ".join(reason_ids))
    user = _prompt(inp)

    resp = client.messages.create(
        model=ONBOARD_MODEL, max_tokens=2000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    data = _parse(_text_of(resp))
    return _build(data, inp)


def _prompt(inp: ProposalInput) -> str:
    parts = []
    if inp.product_name:
        parts.append(f"PRODUCT NAME (hint): {inp.product_name}")
    parts.append("PRODUCT DESCRIPTION / PRICING / DOCS:\n" +
                 (inp.product_text.strip() or "(none provided -- infer conservatively)"))
    parts.append("AUTHORIZED SAVE OFFERS (classify each into one intervention):\n" +
                 "\n".join(f"- {o}" for o in inp.offers))
    return "\n\n".join(parts)


def _parse(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProposalError(f"model did not return valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ProposalError("model output was not a JSON object")
    return data


def _build(data: dict, inp: ProposalInput) -> Proposal:
    interventions = _interventions(data.get("interventions", []))
    if not interventions:
        raise ProposalError("proposal contained no usable interventions")

    try:
        config = ProductConfig(
            product_name=data.get("product_name") or inp.product_name or "Unnamed product",
            product_context=data.get("product_context", ""),
            activation_definition=data.get("activation_definition", ""),
            pricing_summary=data.get("pricing_summary", ""),
            known_churn_reasons=list(data.get("known_churn_reasons", [])),
            competitors=list(data.get("competitors", [])),
            interventions=interventions,
            reasons=list(DEFAULT_REASONS),   # default SaaS taxonomy
            # policy left at defaults -- preferred/discount/let_go/scoring/corroboration.
        ).validate()
    except ValueError as e:
        raise ProposalError(f"proposed config failed validation: {e}") from e

    return Proposal(config=config, notes=str(data.get("notes", "")), raw=data)


def _interventions(raw_list) -> list[Intervention]:
    """Turn the model's proposed offers into Interventions, guaranteeing unique ids."""
    seen: set[str] = set()
    out: list[Intervention] = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict) or not item.get("type") or not item.get("description"):
            continue
        base = (item.get("id") or f"{item['type']}_{i}").strip() or f"offer_{i}"
        iid, n = base, 1
        while iid in seen:
            n += 1
            iid = f"{base}_{n}"
        seen.add(iid)
        out.append(Intervention(
            id=iid, type=str(item["type"]), description=str(item["description"]),
            eligible_when=(item.get("eligible_when") or None),
            params=(item["params"] if isinstance(item.get("params"), dict) else {}),
        ))
    return out
