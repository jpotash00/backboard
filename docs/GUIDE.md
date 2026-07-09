# Offboard — Integration & Design Guide

An AI exit interview that lives **inside your cancel flow**. It diagnoses why a user is
*actually* leaving — not the cover story they open with — then lets deterministic,
server-side policy authorize the right intervention from *your* pre-approved menu.

This guide has two halves:

- **Use it** — [front-end SDK](#2-front-end-the-sdk), [back-end API](#3-back-end-the-engine-api), [onboarding a company](#4-onboarding-a-company-the-config-model).
- **Understand it** — [how it's designed](#5-how-its-designed).

---

## 1. The one thing to understand first

Every integration point returns an **`Outcome`**, and its whole value is two fields side by side:

```
cover_story   what they SAID first     e.g. "too_expensive"
reason        what's ACTUALLY true     e.g. "never_activated"
```

A user who says *"too expensive"* but never activated has a **value** problem wearing a
**price** costume — and a discount is the wrong medicine. The product is that gap.

The model **diagnoses** the `reason`. It never chooses the action. A deterministic
**policy** maps the reason to an `intervention_id` drawn only from actions you authorized.
That split is the entire safety story (§[5.2](#52-the-safety-split-llm-diagnoses-policy-authorizes)).

---

## 2. Front-end: the SDK

The npm package `offboard` renders the interview modal, runs the ≤3-question conversation
against the engine, and hands you a structured `Outcome`. You never touch the LLM.

> **See it run first.** [`demo/`](../demo/) is a mock billing page that drives the real
> SDK against the real engine — cancel click → in-flow interview → diagnosis → authorized
> offer — and toggles between two config-driven products (a SaaS tool and a meditation
> app). It's the fastest way to watch the whole loop end to end. See
> [demo/README.md](../demo/README.md); start the API with `OFFBOARD_CONFIG_DIR=configs` so
> both demo customers load.

### Install & initialize

```bash
npm install offboard
```

```js
import Offboard from "offboard";

// Once, near app startup.
Offboard.init({
  publicKey: "pk_live_...",           // your publishable key
  // apiBaseUrl: "https://api.offboard.dev/v1"  // override for self-hosting
});
```

### Show the flow when the user clicks "Cancel"

```js
cancelButton.addEventListener("click", () => {
  Offboard.showCancelFlow({
    userId: "user_123",

    // Optional behavioral context. The richer this is, the sharper the diagnosis —
    // this is what lets the interviewer see through the cover story.
    context: {
      plan: "Starter",
      mrr: 49,
      tenure_days: 210,
      logins_last_30d: 27,
      activated: true,
      usage_summary: "Daily active; repeatedly hitting the event cap.",
      // Product-specific tells that don't fit the fixed fields:
      signals: { events_this_month: 42000, seats_used: 3 },
    },

    // Optional: the interview concluded — the diagnosis is in (analytics). Fires before
    // the offer step. The SDK shows the offer in-chat; you don't render it yourself.
    onResolved: (outcome) => track(outcome),

    // The user ACCEPTED the offer shown in-chat. THIS is where your code applies it.
    // Offboard hands you the intervention; it never touches your billing.
    onAccept: (outcome) => applyOffer(outcome),

    // The user is LEAVING — declined the offer, none was authorized, or tapped the escape
    // hatch. Complete the cancellation.
    onCancel: (outcome) => completeCancellation(),
  });
});
```

### The SDK shows the offer; you apply it

When policy authorizes an offer, the modal presents it **in the chat** with Accept / No-thanks
buttons. You don't render the offer — you react to the user's choice. On accept, `onAccept`
receives a **`ResolvedOutcome`** (the `Outcome` plus the resolved `intervention`
`{ id, type, description }`), and **you** apply it — Offboard never calls Stripe:

```js
async function applyOffer(outcome) {
  // Branch on the specific intervention id from YOUR config's menu, and do the money.
  switch (outcome.intervention.id) {
    case "discount_50_3mo":
      await stripe.subscriptions.update(subId, { coupon: "HALF_OFF_3MO" });  // your call
      break;
    case "setup_call_15m":
      await scheduling.book("onboarding-15m", outcome /* has the userId context */);
      break;
    case "pause_3mo":
      await stripe.subscriptions.update(subId, { pause_collection: { behavior: "void" } });
      break;
    // ... one case per intervention id in your menu ...
  }

  // `outcome.mode` tells you how far the engine was willing to go on its own:
  //   "act"     — confident enough to auto-apply (as above)
  //   "suggest" — recommend; you may route to a human/ops before charging
  // Plus the full analytics + audit: outcome.reason, cover_story, confidence, economics,
  // decision_trace — the same object also arrived at onResolved.
}
```

> **Route on `intervention.id`, not on `reason`.** The reason is a diagnosis; the
> intervention is the authorized action. Two customers can map the same reason to different
> actions. `outcome.intervention_id` (the bare id) is also present for convenience.

### React

```jsx
function CancelButton({ user }) {
  const onClick = () =>
    Offboard.showCancelFlow({
      userId: user.id,
      context: { plan: user.plan, mrr: user.mrr, activated: user.activated },
      onAccept: (o) => applyOffer(o),                  // they took the save -> your billing
      onCancel: () => router.push("/cancel/confirm"),  // declined / none / escape hatch
    });
  return <button onClick={onClick}>Cancel subscription</button>;
}
```

Call `Offboard.init()` once at app startup (e.g. in your root layout / `main.tsx`).

### Headless (no modal, your own UI)

If you want to render the conversation yourself, drive the same two endpoints directly
with the exported client:

```ts
import { SessionClient } from "offboard";

const client = new SessionClient("https://api.offboard.dev/v1", "pk_live_...");

const { session_id, message } = await client.open({ user_id: "user_123", plan: "Starter", activated: false });
// show `message`, collect a reply, then:
let res = await client.turn(session_id, "it's just too expensive");
while (!res.done) {
  // show res.message, collect the next reply
  res = await client.turn(session_id, nextReply);
}
console.log(res.outcome);       // the Outcome (reason, cover_story, confidence, ...)
console.log(res.intervention);  // the resolved offer { id, type, description } | null
```

The turn response carries both `outcome` and the resolved `intervention`. (The modal
merges them into the single `ResolvedOutcome` it passes to `onResolved`.)

---

## 3. Back-end: the engine API

A thin FastAPI server wraps the interviewer + policy behind a session API. The SDK talks
to it; you host it (or use the managed endpoint).

### Run it

```bash
pip install -e ".[api]"
export ANTHROPIC_API_KEY=sk-ant-...
python -m api.app            # serves on :8000 (PORT env to change)
```

### Auth

Every request carries `Authorization: Bearer <public_key>`. The key resolves to a
**customer** (and thus their `ProductConfig`) in the registry. Out of the box the demo
key `pk_demo_acme` is seeded; point at real configs with `OFFBOARD_CONFIG_DIR` (§4).

### Endpoints (§4 of the brief)

| Method & path | Body | Returns |
|---|---|---|
| `GET /health` | — | `{ "status": "ok" }` |
| `POST /sessions` | `UserContext` (only `user_id` required) | `{ session_id, message }` |
| `POST /sessions/{id}/turn` | `{ user_message }` | `{ message, done }` **or** `{ done: true, outcome, intervention }` |
| `POST /sessions/{id}/resolution` | `{ accepted }` | `{ status, accepted }` — logs the realized save |

The SDK calls `/resolution` for you when the user accepts or declines the in-chat offer;
call it yourself only if you drive the session headless. It's idempotent, and it appends a
self-contained record to `runs/resolutions.jsonl` (reason, intervention, `offered`,
`accepted`) — the ground truth for save rates and for recalibrating the policy's priors.

The turn endpoint is **idempotent** once resolved: calling it again on a finished session
replays the stored outcome rather than re-diagnosing.

### End-to-end with curl

```bash
# Open a session
curl -s localhost:8000/sessions -H "Authorization: Bearer pk_demo_acme" \
  -H 'content-type: application/json' \
  -d '{"user_id":"u1","plan":"Starter","mrr":49,"tenure_days":210,
       "logins_last_30d":27,"activated":true,
       "usage_summary":"Daily active; constantly hitting the event cap."}'
# -> { "session_id": "...", "message": "Sorry to see you go — what's prompting this?" }

# Answer turns until {"done": true, "outcome": {...}}
curl -s localhost:8000/sessions/<session_id>/turn -H "Authorization: Bearer pk_demo_acme" \
  -H 'content-type: application/json' -d '{"user_message":"it is too expensive"}'
```

Every completed session is appended to `runs/sessions.jsonl` (full transcript + outcome)
— the data asset that compounds (§[5.4](#54-the-data-asset)).

---

## 4. Onboarding a company (the config model)

> **You rarely author a config from scratch — you review a proposed one.** The
> `onboarding.propose_config` step takes your pasted pricing/docs plus the list of saves
> you're authorized to offer, and returns a **validated `ProductConfig`** to approve or
> tweak. The LLM only *structures* the input; the taxonomy and policy come from validated
> defaults, and `validate()` is the backstop.
>
> ```python
> from onboarding import propose_config, ProposalInput
> proposal = propose_config(ProposalInput(
>     offers=["30% off for 3 months", "free onboarding call", "pause up to 2 months"],
>     product_text=pasted_pricing_and_docs,
> ))
> proposal.config      # a validated ProductConfig, ready to review
> proposal.notes       # assumptions the model made — what the human should check
> ```

The rest of this section is the config a proposal produces (and what you'd hand-edit).

Everything company-specific lives in a **`ProductConfig`**. The engine, API, and SDK are
generic; swap the config and the same code becomes a different company's cancel flow.
Nothing in the engine is SaaS-specific except the *defaults*.

### Anatomy of a config

```python
from engine import ProductConfig, ReasonDef, Intervention, Policy

config = ProductConfig(
    # --- product facts (rendered into the interviewer's context) ---
    product_name="Acme Analytics",
    product_context="Self-serve product analytics for small SaaS teams.",
    activation_definition="connected a data source and viewed one dashboard",
    pricing_summary="$49/mo Starter, $199/mo Growth",
    known_churn_reasons=["signed up but never connected a source", "outgrew us"],
    competitors=["Amplitude", "Mixpanel"],

    # --- the churn taxonomy the model must resolve into (defaults to the SaaS 8) ---
    reasons=[
        ReasonDef("never_activated", "signed up, never reached first value"),
        ReasonDef("price_value_mismatch", "got real value, doesn't justify cost"),
        # ... your reasons; each `description` IS the prompt spec for that reason ...
    ],

    # --- the resolution menu: actions YOU have authorized. The model can't invent one ---
    interventions=[
        Intervention(id="discount_50_3mo", type="discount", description="50% off for 3 months",
                     eligible_when="reason == price_value_mismatch AND tenure > 90"),
        Intervention(id="setup_call_15m", type="onboarding", description="Free 15-min setup call"),
    ],

    # --- the business rulebook: reason -> which resolution, and when ---
    policy=Policy(
        confidence_floor=0.6,                       # below this, defer to your generic flow
        preferred={                                 # reason id -> resolution types, best first
            "never_activated":      ["onboarding", "support"],
            "price_value_mismatch": ["discount", "downgrade"],
        },
        discount_reasons={"price_value_mismatch"},  # reasons a discount is legitimate for
        let_go_reasons={"value_ended"},             # reasons to let go cleanly (offer a pause)
    ),
    config_version="1",
)
```

Anything you omit inherits a sensible default (the standard SaaS taxonomy and rulebook),
so a minimal config is just the four product-fact fields.

**The `eligible_when` mini-language** is a safe `OR`-of-`AND`s of `field OP value` atoms
(no code execution). Fields available: `reason`, `confidence`, `tenure`, `mrr`,
`activated`, `logins`, plus any custom `signals`. Example:
`"reason == price_value_mismatch AND tenure > 90"`.

### Store it as JSON (the production path)

Configs are stored, versioned records — a file today, a DB row later. One file per
customer:

```jsonc
// configs/acme.json
{
  "customer_id": "acme",
  "public_key": "pk_demo_acme",
  "config": { /* the ProductConfig, serialized — see engine.config_to_dict */ }
}
```

Generate one from a Python config, or hand-author it:

```python
import json
from engine import config_to_dict, config_from_dict

json.dump(config_to_dict(config), open("configs/acme.json", "w"))  # serialize
loaded = config_from_dict(json.load(open("configs/acme.json")))    # validates on load
```

Point the server at the directory and it loads every `*.json` as a customer:

```bash
export OFFBOARD_CONFIG_DIR=configs
python -m api.app
```

### Validation

`config_from_dict` (and `Interviewer` construction) calls `config.validate()`, which
**fails fast** on an inconsistent config — an unknown reason id in the policy, a
confidence floor outside `[0, 1]`, duplicate ids, a reason with no policy entry. A broken
stored config raises at load, never mid-interview. Accept configs from a form or the
proposer with confidence.

---

## 5. How it's designed

### 5.1 Three layers, one contract

```
sdk/     (TypeScript, browser)   renders the modal, relays turns, surfaces the Outcome
  │  HTTP: POST /sessions, POST /sessions/:id/turn   (Bearer pk_...)
  ▼
api/     (FastAPI)               auth -> session state -> orchestration; framework-thin
  │  in-process calls
  ▼
engine/  (pure Python)           the durable spine — reused unchanged by API and SDK
  ├─ taxonomy.py   ProductConfig / Policy / ReasonDef / Intervention / Outcome + validate
  ├─ interviewer.py free-form probing, ≤3 turns, emits a STRUCTURED diagnosis (JSON)
  ├─ policy.py      reason -> authorized intervention (deterministic; no model)
  └─ serialization.py  config <-> JSON, validates on load
```

The **`Outcome`** shape is the single contract that crosses every boundary. It's defined
once in [`engine/taxonomy.py`](../engine/taxonomy.py) and mirrored in
[`sdk/src/types.ts`](../sdk/src/types.ts) — keep them in lockstep.

### 5.2 The safety split: LLM diagnoses, policy authorizes

This is the load-bearing design decision. The full treatment — the economic model, the
`expected_value` ranking mode, and the declared decision trace — is in
**[DECISIONING.md](DECISIONING.md)**.

- The **interviewer** (LLM) may only ask questions and emit a diagnosis: a `reason`, a
  `confidence`, and the `evidence`. It is structurally incapable of granting anything —
  its JSON output has no "give discount" field.
- The **policy** ([`engine/policy.py`](../engine/policy.py)) is deterministic Python. It
  takes the diagnosis and selects an `intervention_id` **only** from the customer's
  authorized menu, gated by `confidence_floor` and `eligible_when`.

Consequences that are guaranteed, not hoped for:

- A `never_activated` user can **never** be handed a discount (discounting someone who
  never got value is strictly worse than helping them activate).
- Below the confidence floor, `intervention_id` is `null` and control returns to your
  generic flow — a confident-and-wrong save is the dangerous case, so uncertainty defers.
- `value_ended` is let go cleanly with a pause, warm door open — not bribed to stay.

### 5.3 End-to-end sequence

```
user clicks Cancel
      │
  SDK  Offboard.showCancelFlow({ userId, context })
      │  POST /sessions  (Bearer pk_...)
  API  authenticate -> resolve customer + ProductConfig
      │  Interviewer(config, user).open()      # config.validate() runs here
      │  <- first question
  SDK  render modal, user replies
      │  POST /sessions/:id/turn { user_message }   (repeat ≤3x)
  API  interviewer.turn(reply)
      │    ├─ "ask"      -> next question  -> { message, done:false }
      │    └─ "diagnose" -> Outcome(reason, confidence, evidence, cover_story, ...)
      │  decide(outcome, config, user)         # policy fills intervention_id + rationale
      │  resolve intervention_id -> the offer { id, type, description }
      │  log transcript+outcome -> runs/sessions.jsonl
      │  <- { done:true, outcome, intervention }
  SDK  onResolved(outcome)                  # analytics; then, if an offer was authorized:
  SDK  present offer in-chat [Accept] [No thanks]
       ├─ Accept    -> onAccept(outcome)     -> YOU apply it (call Stripe, book call, ...)
       └─ No thanks -> onCancel(outcome)     -> YOU complete the cancellation
```

The interviewer is **bounded**: at `MAX_TURNS` (3) it is forced to diagnose with the
evidence it has, using low confidence if genuinely unsure. The UI just renders whatever
the server returns and closes on `done` — it can't run away.

### 5.4 The data asset

Every completed session is appended to `runs/sessions.jsonl` with the full transcript and
the outcome. This is the compounding asset: the transcripts *where the interviewer got
stuck are the product spec* for the next iteration of the prompt, and the labeled
(cover_story, reason) pairs are training/eval data you can't buy.

### 5.5 Per-company tailoring — the seams

| What differs per company | Where it lives | Default |
|---|---|---|
| Product facts, pricing, competitors | `ProductConfig` fields | — |
| Churn taxonomy (the reasons) | `ProductConfig.reasons` (`ReasonDef`) | standard SaaS 8 |
| Resolution menu | `ProductConfig.interventions` | — |
| Reason → action rulebook | `ProductConfig.policy` (`Policy`) | standard SaaS map |
| Confidence floor | `Policy.confidence_floor` | `0.6` |
| Eligibility rules | `Intervention.eligible_when` | always eligible |
| Behavioral signals | `UserContext` + `signals` dict | generic fields |

Two companies with the *same* taxonomy can run *opposite* policies. A non-SaaS company
(say a snack-box subscription) supplies its own `reasons` and `policy` and the engine
treats reason ids as opaque strings throughout — nothing in the engine changes.

---

## 6. Reference

### `Outcome`

| Field | Type | Meaning |
|---|---|---|
| `reason` | string | the diagnosed real reason (a config reason id) |
| `confidence` | number 0..1 | below `confidence_floor` ⇒ `intervention_id` is null |
| `evidence` | string | the specific thing said/observed that proves it |
| `cover_story` | string | what they claimed first |
| `savable` | boolean | policy's judgment on whether a save is worth attempting |
| `intervention_id` | string \| null | the authorized action's id, or null (fall back) |
| `rationale` | string | human-readable "why this action" |
| `turns_used` | number | questions it took (≤ 3) |
| `mode` | string | `defer` \| `suggest` \| `act` — whether the offer is cleared to auto-apply or should be recommended for review ([DECISIONING.md §2.2](DECISIONING.md)) |
| `corroboration` | object | did the behavioral data back the diagnosis; the confidence it adjusted to |
| `economics` / `decision_trace` | object / array | the declared, auditable decision record |

`onResolved`, `onAccept`, and `onCancel` all receive a **`ResolvedOutcome`** = the
`Outcome` above plus a resolved `intervention` object (`{ id, type, description }`, or
`null` when policy authorized nothing). The SDK presents the offer in-chat; `onAccept`
fires when the user takes it (your cue to apply it), `onCancel` when they don't.

| `intervention` field | Type | Meaning |
|---|---|---|
| `id` | string | matches `intervention_id` |
| `type` | string | `discount` \| `onboarding` \| `pause` \| `downgrade` \| ... (open) |
| `description` | string | display-ready copy, e.g. `"50% off for 3 months"` |

### `UserContext` / `POST /sessions` body

`user_id` (required), `plan`, `mrr`, `tenure_days`, `logins_last_30d`, `activated`,
`usage_summary`, and `signals` (a free-form object of product-specific tells, e.g.
`{ "seats_used": 7 }`, rendered into the interviewer's context). Richer context ⇒ sharper
diagnosis.

### Environment variables

| Var | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required to run the interviewer |
| `OFFBOARD_CONFIG_DIR` | — | load customers from JSON configs; else demo `pk_demo_acme` |
| `CHURN_MODEL` | `claude-sonnet-5` | the interviewer model |
| `PORT` | `8000` | API port |

---

## 7. Not yet wired (honest edges)

- **Custom-taxonomy typing in the SDK.** `Reason` in `sdk/src/types.ts` is the default
  SaaS union. If a customer runs a custom taxonomy, treat `outcome.reason` as an opaque
  string on the client.
- **Config admin endpoint.** Onboarding currently means dropping a validated JSON file in
  `OFFBOARD_CONFIG_DIR`. A `POST /configs` (propose → validate → persist) would remove the
  need for file access.
- **Session store durability.** Sessions are in-memory (the store interface is
  Redis-shaped); a live interview holds the `Interviewer` object, so it's process-local
  until the store is backed by Redis.
```
