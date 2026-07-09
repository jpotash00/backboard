# Offboard — Build Brief

> Paste this whole file into Claude Code as the project spec.

---

## 0. Read this first (the part that matters)

**Do not build the SDK until Milestone 1 passes.**

Everything in this system is scaffolding around one unproven claim: *an LLM, given three questions and behavioral data, can find a churner's real reason when their opening line is a cover story.* If that fails, the SDK, the API, the integrations, and the dashboard are a beautiful wrapper around nothing.

Milestone 1 is a blind eval that answers this in about an hour. Run it. Look at the transcripts. Then decide whether to build the rest.

---

## 1. What this is

An AI exit interview that lives **inside the cancel flow**, diagnoses why the person is *actually* leaving, and picks the right intervention in real time.

**The insight:** blanket save offers are a margin bonfire. Everyone shows the same 50%-off to everybody. That pays to retain people who were never leaving, pays people who take the discount and churn anyway, and hands money to the person whose real problem was that they never finished onboarding. Meanwhile the cancel-flow dropdown ("too expensive / missing feature / other") can't tell those apart.

**The claim:** *"Too expensive" is the great lie of churn.* It's socially safe and always plausible. A person who says it but never activated does not have a price problem — they have a value problem, and a discount is the wrong medicine. The product is the gap between the **cover story** and the **reason**.

**Positioning:** research tools (User Intuition, Enterpret) have depth and no action. Deflection tools (Churnkey, ProsperStack) have action running on a dropdown's worth of reasoning. This does both, in one loop, in-flow.

---

## 2. Architecture

**Fixed spine, adaptive probing.** The conversation is free-form; the destination is not.

- **Deterministic:** the churn taxonomy (stable across every software type — only vocabulary differs), the intervention menu, the eligibility gates.
- **LLM:** how you get there. You cannot script the probes, because the whole thesis is that stated reasons are cover stories. "Too expensive" needs a different follow-up depending on what preceded it.

**What differs per customer is config, not questions.** Product context, activation definition, pricing, known churn reasons, competitors, and — critically — *which interventions they've authorized*. No point diagnosing "needs a setup call" if they don't offer one.

**LLM diagnoses. Policy authorizes.** The model never hands out a discount. It emits a structured diagnosis; deterministic rules map it to an action from the customer's own menu. Two reasons: nobody lets a language model authorize 50%-off unsupervised, and structured output is what makes the data aggregate later.

```
cancel click
   │
   ▼
[SDK modal] ──POST /sessions──────────► [engine] open()
   │  ◄─────────── first question ──────────┘
   │
   ├──POST /sessions/:id/turn ─────────► interviewer.turn()
   │  ◄──── next question (≤3 total) ───────┘
   │
   └──────────────────────────────────► outcome
                                          │
                                    policy.decide()   ← deterministic
                                          │
                                    { reason, cover_story, confidence,
                                      evidence, savable, intervention_id }
```

---

## 3. Hard design constraints

Each of these is load-bearing. Don't relax them without a reason.

1. **MAX_TURNS = 3.** Patience is the binding constraint, not laddering depth. Every extra probe costs completion — and completion loss is *non-random*: you lose exactly the disengaged churners you most need to understand. Measure completion rate as a first-class metric next to diagnostic accuracy.

2. **Chat, not voice.** They came to cancel. It's 11pm, they're on a phone, possibly beside a sleeping partner. Voice demands they be somewhere they can talk at the exact moment they're trying to leave. Everyone can type; only some can speak. (Voice makes sense for scheduled, recruited, paid research — that's why User Intuition uses it. This is operational, not research.)

3. **The escape hatch is always visible.** One tap: *Just cancel.* Counterintuitively this is what makes the data credible — someone who chose to answer is telling the truth; someone cornered will type anything to escape. It's also what keeps you off the dark-patterns screenshot list.

4. **Never guilt-trip, never negotiate.** The interviewer's job is to understand, not to save. Saving is policy's job, downstream.

5. **Confidence floor (0.6).** Below it, return no intervention and let the caller fall back to their generic flow. An honest "I don't know" is worth more than a confident guess.

6. **Behavioral data disambiguates the cover story.** "Too expensive" from a daily power user is real. From someone with 2 logins in 60 days who never activated, it's a value problem in a price costume. The engine must see usage.

---

## 4. Core contracts

### Taxonomy (resolve to exactly one)
```
never_activated       signed up, never reached first value
value_ended           need genuinely finished (project done, left company, seasonal)
price_value_mismatch  got real value, doesn't justify cost
missing_capability    needed something we don't do
switched_competitor   someone else won them
product_quality       bugs, reliability, support failures
involuntary           payment failure — not a real churn decision
unknown               cannot resolve — fall back
```

### Session API
```
POST /sessions
  { user_id, plan, mrr, tenure_days, logins_last_30d, activated, usage_summary }
  → { session_id, message }

POST /sessions/:id/turn
  { user_message }
  → { message, done: false }
  → { done: true, outcome }
```

### Outcome (this is the product)
```json
{
  "reason": "never_activated",
  "confidence": 0.82,
  "evidence": "Never connected a data source; 3 logins in 60 days",
  "cover_story": "too_expensive",
  "savable": true,
  "intervention_id": "setup_call_15m",
  "rationale": "never_activated -> onboarding: 15-min setup call",
  "turns_used": 2
}
```

`cover_story` vs `reason` is the entire pitch in one field: *"they said price, they meant activation."* An engineer reads that and instantly gets it. `confidence` lets them set a threshold and adopt safely.

### Customer config (registered once)
```json
{
  "product_name": "Acme Analytics",
  "product_context": "Self-serve product analytics for small SaaS teams",
  "activation_definition": "connected a data source and viewed one dashboard",
  "pricing_summary": "$49/mo Starter, $199/mo Growth",
  "known_churn_reasons": ["never connected data source", "outgrew to Amplitude"],
  "competitors": ["Amplitude", "Mixpanel", "PostHog"],
  "interventions": [
    {"id":"discount_50_3mo","type":"discount","description":"50% off for 3 months",
     "eligible_when":"reason=price AND tenure>90"},
    {"id":"pause_3mo","type":"pause","description":"Pause subscription 3 months"},
    {"id":"setup_call_15m","type":"onboarding","description":"15-min setup call"},
    {"id":"roadmap_notify","type":"roadmap","description":"Notify when feature ships"}
  ]
}
```

**Onboarding a customer is the hidden product.** If setup requires writing a 500-word product description, adoption dies. The elegant version: scrape their pricing page + docs, propose the config, they approve in five minutes. This will matter more to conversion than interview quality does.

### Policy rules (deterministic, auditable)
- `discount` is only reachable from `price_value_mismatch`. **Never** from `never_activated` — discounting someone who never got value is strictly worse than helping them get it.
- `value_ended` → offer a pause, mark unsavable, warm door open. Fighting this burns margin and goodwill for nothing.
- `involuntary` → just fix the card. Not a real churn decision.
- Every authorized discount must be traceable to a rule an engineer can audit.

**Framing note for GTM:** sell this as *offer efficiency* (same saves, less margin spent), never as *withheld offers*. "Don't try to save this person" is economically right, emotionally hard to buy, and unfalsifiable — you can't prove the one you let go was unsavable.

---

## 5. Milestone 1 — The eval (BUILD THIS FIRST)

Ten synthetic churners. Each has a **hidden true reason** and a **plausible cover story** they open with. The interviewer runs blind — it sees only the cover story and the behavioral data. Then score.

### Personas (build all ten)
| # | Hidden reason | Opens with | Behavioral tell |
|---|---|---|---|
| 1 | `never_activated` | "too expensive" | 2 logins/60d, not activated |
| 2 | `price_value_mismatch` | "too expensive" | daily user, activated, hitting limits |
| 3 | `value_ended` | "not using it anymore" | heavy use then cliff to zero |
| 4 | `switched_competitor` | "too complicated" | activated, moderate use, cancelled 3d after competitor launch |
| 5 | `missing_capability` | "too expensive" | activated, power user, repeated failed searches for one feature |
| 6 | `product_quality` | "not using it" | activated, 6 support tickets, declining use |
| 7 | `involuntary` | "no_reason_given" | active daily, card expired |
| 8 | `never_activated` | "found an alternative" | 1 login, never activated |
| 9 | `value_ended` | "too expensive" | left the company (says "we" → "I don't work there") |
| 10 | `price_value_mismatch` | "missing feature" | activated, downgraded once already, mentions budget twice |

Personas 1 vs 2 are the crux — **identical cover story, opposite diagnosis, opposite correct intervention.** If the system can't split those two, it doesn't work. Personas 5 and 10 are the hard ones (cover story and truth are both partly true).

Each persona is itself an LLM roleplay: give it the hidden reason, a personality, and instructions to *behave like a real churner* — mildly annoyed, brief, defensive about the real reason, will admit it if asked well, will not volunteer it.

### Scoring
- **Diagnostic accuracy** — exact match on `reason`. This is the headline number.
- **Cover-story penetration** — accuracy *restricted to* personas where cover_story ≠ reason (all ten, by design). This is the real metric. Anything that just believes the stated reason scores ~0 here.
- **Intervention correctness** — did policy pick the right action?
- **Calibration** — is confidence high when right, low when wrong? A system that's wrong *and* confident is worse than useless.
- **Turns used** — did it diagnose early when it could?

**Baseline to beat:** a "dropdown" control that simply believes the stated cover story. Report both. The delta *is* the product's value, and it's the number that goes in the deck.

**Bar:** ≥70% cover-story penetration, and personas 1 vs 2 split correctly. Below that, iterate the prompt before building anything else.

Print full transcripts. Read them. The failures will teach you more than the score — where it got stuck is your product spec.

---

## 6. Milestone 2 — Engine + API
FastAPI, endpoints in §4. Session state in memory (Redis later). Structured logging of every transcript + outcome — that's the data asset that compounds.

## 7. Milestone 3 — SDK
Drop-in web SDK first. `npm install`, then:
```js
Offboard.init({ publicKey: "pk_..." });
Offboard.showCancelFlow({ userId, onResolved: (outcome) => {...} });
```
Renders a modal chat. **You own the UI** — that's not convenience, it's control: if they build their own, they can bury the escape hatch, degrade the conversation, and you can't fix it or improve it without their redeploy.

Ship web/Stripe/self-serve-SaaS first. **Mobile is meaningfully harder** — App Store review, subscriptions via Apple/Google IAP rather than the company's billing, users can cancel in iOS Settings and never touch your flow, and every fix ties to their release cycle. Web lets you iterate daily. Prove it there, then port.

## 8. Later (do not build now)
Generic webhook (`POST /churn-event` for Chargebee/Recurly/Paddle/homegrown) — nobody ever failed for supporting Stripe but not Paddle. PostHog/Amplitude ingestion — strategically right (behavior disambiguates the cover story), but you don't need it to learn whether the mechanic works, and building it now means guessing which signals matter instead of letting the eval failures tell you. Aggregate intelligence dashboard. Write-access to billing.

---

## 9. Reference implementation

Working code for the three core modules follows. It runs against `ANTHROPIC_API_KEY`. Use it as the starting point for Milestone 1; the eval harness and personas are yours to write.

### `engine/taxonomy.py`
```python
"""The deterministic spine: a fixed churn taxonomy the LLM must resolve into.
The conversation is free-form. The destination is not."""

from dataclasses import dataclass, field
from typing import Literal, Optional

Reason = Literal[
    "never_activated", "value_ended", "price_value_mismatch", "missing_capability",
    "switched_competitor", "product_quality", "involuntary", "unknown",
]

COVER_STORIES = [
    "too_expensive", "not_using_it", "found_alternative",
    "missing_feature", "too_complicated", "no_reason_given",
]


@dataclass
class Intervention:
    """An action the CUSTOMER has authorized. We can never invent one."""
    id: str
    type: Literal["discount", "pause", "downgrade", "onboarding", "roadmap", "support", "none"]
    description: str
    eligible_when: Optional[str] = None


@dataclass
class ProductConfig:
    """Injected per-customer. This -- not the questions -- is what differs between
    a design tool and a meditation app."""
    product_name: str
    product_context: str
    activation_definition: str
    pricing_summary: str
    known_churn_reasons: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    interventions: list[Intervention] = field(default_factory=list)


@dataclass
class UserContext:
    """Behavioral signal. This is what disambiguates the cover story."""
    user_id: str
    plan: str
    mrr: float
    tenure_days: int
    logins_last_30d: int
    activated: bool
    usage_summary: str = ""


@dataclass
class Outcome:
    """`cover_story` vs `reason` is the entire pitch in one field."""
    reason: Reason
    confidence: float
    evidence: str
    cover_story: str
    savable: bool
    intervention_id: Optional[str]
    rationale: str
    turns_used: int
```

### `engine/interviewer.py`
```python
"""The interviewer. Free path, fixed destination."""

import json
import os
from dataclasses import asdict
from typing import Optional

import anthropic

from .taxonomy import Outcome, ProductConfig, UserContext

MODEL = os.getenv("CHURN_MODEL", "claude-sonnet-5")
MAX_TURNS = 3  # hard ceiling. Raise it and watch completion rate fall.

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
Usage: {usage_summary}

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
  never_activated       -- signed up, never reached first value
  value_ended           -- need genuinely finished (project done, left company, seasonal)
  price_value_mismatch  -- got real value, doesn't justify cost
  missing_capability    -- needed something we don't do
  switched_competitor   -- someone else won them
  product_quality       -- bugs, reliability, support failures
  involuntary           -- payment failure, not a real churn decision
  unknown               -- cannot resolve; be honest about this

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
        self.config = config
        self.user = user
        self.client = client or anthropic.Anthropic()
        self.messages: list[dict] = []
        self.turns = 0

    def _system(self) -> str:
        return SYSTEM.format(
            max_turns=MAX_TURNS,
            product_name=self.config.product_name,
            product_context=self.config.product_context,
            pricing_summary=self.config.pricing_summary,
            activation_definition=self.config.activation_definition,
            known_churn_reasons=", ".join(self.config.known_churn_reasons) or "none recorded",
            competitors=", ".join(self.config.competitors) or "none recorded",
            **{k: v for k, v in asdict(self.user).items() if k != "user_id"},
        )

    def _call(self) -> dict:
        resp = self.client.messages.create(
            model=MODEL, max_tokens=600, system=self._system(), messages=self.messages,
        )
        text = resp.content[0].text.strip()
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
```

### `engine/policy.py`
```python
"""LLM diagnoses. Policy authorizes. This separation is the whole safety story."""

from typing import Optional
from .taxonomy import Intervention, Outcome, ProductConfig, UserContext

CONFIDENCE_FLOOR = 0.6

# Note what's absent: never_activated. Discounting someone who never got value
# is strictly worse than helping them get it.
DISCOUNT_FIXES = {"price_value_mismatch"}

# The honest move is to let them go cleanly, warm door open.
LET_GO = {"value_ended"}

PREFERRED: dict[str, list[str]] = {
    "never_activated":      ["onboarding", "support", "pause"],
    "price_value_mismatch": ["discount", "downgrade", "pause"],
    "value_ended":          ["pause", "none"],
    "missing_capability":   ["roadmap", "none"],
    "switched_competitor":  ["discount", "roadmap"],
    "product_quality":      ["support", "discount"],
    "involuntary":          ["support"],
    "unknown":              [],
}


def _eligible(iv: Intervention, outcome: Outcome, user: UserContext) -> bool:
    if iv.type == "discount" and outcome.reason not in DISCOUNT_FIXES:
        return False
    if not iv.eligible_when:
        return True
    scope = {"reason": outcome.reason, "confidence": outcome.confidence,
             "tenure": user.tenure_days, "mrr": user.mrr,
             "activated": user.activated, "logins": user.logins_last_30d}
    expr = (iv.eligible_when.replace("=", "==").replace(">==", ">=").replace("<==", "<=")
            .replace("AND", "and").replace("OR", "or"))
    for w in ("never_activated", "value_ended", "price_value_mismatch", "missing_capability",
              "switched_competitor", "product_quality", "involuntary", "unknown", "price"):
        expr = expr.replace(f"=={w}", f'=="{w}"')
    expr = expr.replace('=="price"', '=="price_value_mismatch"')
    try:
        return bool(eval(expr, {"__builtins__": {}}, scope))
    except Exception:
        return False


def decide(outcome: Outcome, config: ProductConfig, user: UserContext) -> Outcome:
    if outcome.confidence < CONFIDENCE_FLOOR or outcome.reason == "unknown":
        outcome.intervention_id, outcome.savable = None, False
        outcome.rationale = (f"confidence {outcome.confidence:.2f} below floor "
                             f"{CONFIDENCE_FLOOR} -- deferring to your generic cancel flow")
        return outcome

    if outcome.reason in LET_GO:
        outcome.savable = False
        outcome.intervention_id = _find(config, "pause")
        outcome.rationale = ("need genuinely ended -- offering a pause, not a discount. "
                             "Spending margin here retains no one.")
        return outcome

    for wanted in PREFERRED.get(outcome.reason, []):
        iv = _find_obj(config, wanted)
        if iv and _eligible(iv, outcome, user):
            outcome.intervention_id = iv.id
            outcome.rationale = f"{outcome.reason} -> {iv.type}: {iv.description}"
            return outcome

    outcome.intervention_id, outcome.savable = None, False
    outcome.rationale = f"no authorized intervention matches {outcome.reason}"
    return outcome


def _find(config: ProductConfig, type_: str) -> Optional[str]:
    iv = _find_obj(config, type_)
    return iv.id if iv else None


def _find_obj(config: ProductConfig, type_: str) -> Optional[Intervention]:
    return next((i for i in config.interventions if i.type == type_), None)
```

---

## 10. Your first command to Claude Code

> Read this spec. Implement **Milestone 1 only**: `eval/personas.py` (ten synthetic churners per §5, each an LLM roleplay with a hidden reason and a defended cover story) and `eval/run_eval.py` (blind harness + the five scores + the dropdown baseline). Use the reference implementation in §9 as-is. Print full transcripts. Do not build the API or SDK yet.

Then read the transcripts. Where it got stuck is the product spec.
