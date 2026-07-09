# Offboard — The Decisioning Framework

How Offboard decides what to offer a churning user, and why that decision is
**deterministic and declared, never prompted**. This is the reference for the principle:

> **Offboard suggests the principal decision-making; you fine-tune it; every output is declared.**

---

## 1. Two decisions — only one is an LLM

The most important thing to understand: there are two separate decisions, and they live in
two different places on purpose.

| Decision | Question | Who decides | How |
|---|---|---|---|
| **Diagnosis** | *What's the real reason they're leaving?* | the LLM | a prompt ([`engine/interviewer.py`](../engine/interviewer.py)) |
| **Authorization** | *What should we do about it?* | deterministic policy | **code + config** ([`engine/policy.py`](../engine/policy.py)) — **no prompt** |

**You never write a prompt for "decide how best to proceed given the constraints."** That
would hand a language model the authority to spend money, which breaks the three
guarantees the product is sold on:

1. **Auditability** — "every authorized discount traceable to a rule an engineer can
   audit." A prompt's reasoning is not a rule; it varies run to run.
2. **No margin bonfire** — a hallucinating model could hand 50%-off to someone who never
   activated. Deterministic policy *structurally cannot*.
3. **Aggregation** — structured decisions roll up into offer-efficiency numbers.

The LLM emits only a diagnosis (`reason`, `confidence`, `evidence`). It has no field with
which to grant anything. Policy takes that diagnosis and authorizes an action — and now it
does so with **visible economics**.

---

## 2. The decision pipeline

`decide(outcome, config, user)` runs a fixed sequence. Every branch writes a full audit
trail onto the outcome (`economics` + `decision_trace`).

```
1. CONFIDENCE FLOOR   confidence < floor (or reason == unknown)?
                      -> authorize nothing; defer to the host's generic flow.
                      (An honest "I don't know" beats a confident wrong save.)

2. LET-GO             reason in policy.let_go_reasons (e.g. value_ended)?
                      -> offer a pause, mark unsavable. Don't fight it; warm door open.

3. SCORE              for each option in policy.preferred[reason]:
                        - is it in the menu?         (else: rejected, declared)
                        - is it eligible?            (discount gate + eligible_when rule)
                        - does confidence clear its per-type bar?
                        - compute cost + expected value
                      Every option — chosen or rejected — is recorded with the reason.

4. SELECT             rank_by == "preferred"      -> first eligible in the curated order
                      rank_by == "expected_value" -> highest EV among eligible
```

Nothing here is a model call. Given the same diagnosis and config, the decision is
identical every time.

---

## 3. The suggested economics (`Policy.scoring`)

The economic model is **opinionated defaults you tune, not logic you rewrite**. It has two
jobs: **declare** the economics of every decision, and (in `expected_value` mode) **rank**
the options.

```
Expected value of an action:

  EV = P(saveable | reason) × P(this action converts | type) × customer_value − cost(type)
       └── save_prior ──┘    └── type_effectiveness ──┘       └ mrr × horizon ┘   └ type_cost ┘
```

Suggested defaults (all in [`engine/taxonomy.py`](../engine/taxonomy.py) → `Scoring`):

| Knob | What it is | Example defaults |
|---|---|---|
| `type_cost` | $ cost of *making* the offer | discount 75, downgrade 30, onboarding 15, roadmap 0 |
| `save_prior` | P(this **reason** is saveable at all) | price 0.50, never_activated 0.35, value_ended 0.10, involuntary 0.90 |
| `type_effectiveness` | how well an action **type** converts a saveable churner (0..1) | discount 0.9, downgrade 0.7, pause 0.5, roadmap 0.3 |
| `value_horizon_months` | LTV proxy horizon | 12 |
| `min_confidence_by_type` | per-type confidence bar (costly ⇒ stricter) | *(empty; global floor governs)* |

> **Why two probability terms?** Without `type_effectiveness`, EV would just minimize cost
> — every action shares the reason's save odds, so the cheapest always wins. Effectiveness
> is what makes "a discount converts a price churner better than a pause" show up in the math.

---

## 4. `rank_by`: the safe default vs. the efficient upgrade

| Mode | Behavior | When |
|---|---|---|
| `"preferred"` *(default)* | Pick the first eligible action in the curated per-reason order. Deterministic, obvious, easy to reason about. | Ships on by default. |
| `"expected_value"` | Pick the highest-EV eligible action. Spends margin only where it pays back. | Opt in when you trust your cost/value numbers. |

The default is deliberately the curated order, so turning Offboard on doesn't silently
change anyone's payouts. **`expected_value` is a one-line config change** — and it's where
"same saves, less margin spent" becomes real. Example: a **$8/mo** price churner.

```jsonc
// rank_by: "expected_value"  →  intervention_id: "pause_3mo"
"economics": { "customer_value": 96, "save_probability": 0.5, "margin_spent": 10.0,
               "chosen_expected_value": 14.0 },
"decision_trace": [
  { "type": "discount",  "cost": 75, "effectiveness": 0.9, "expected_value": -31.8, "eligible": true },
  { "type": "downgrade", "cost": 30, "effectiveness": 0.7, "expected_value":   3.6, "eligible": true },
  { "type": "pause",     "cost": 10, "effectiveness": 0.5, "expected_value":  14.0, "eligible": true, "chosen": true }
]
```

The discount scores **negative** EV — it costs more margin than this low-value user is
worth — so EV mode declines it and offers a pause instead. In `"preferred"` mode the same
user would have gotten the discount. That delta is the product.

---

## 5. The declared output

Every `Outcome` carries the decision, spelled out, so spend is auditable and analyzable.
It flows unchanged through the API turn response and into `runs/sessions.jsonl`.

- **`economics`** — `customer_value`, `save_probability`, `rank_by`, `margin_spent`, and
  (when an action fires) `chosen_expected_value`.
- **`decision_trace`** — one entry per option considered: its `type`, `cost`,
  `effectiveness`, `expected_value`, `required_confidence`, `eligible`, a `rejected`
  reason when excluded, and `chosen: true` on the winner. A gated-off discount **says so**
  (`"rejected": "ineligible (discount-reason gate)"`) rather than silently vanishing.

This is the "output declared" half of the principle: you can always answer *why did this
user get this offer, and what did the alternatives cost?* — without re-running anything.

---

## 6. Fine-tuning — the seams

Everything below is per-customer config; the engine code is untouched.

| To change… | Set… |
|---|---|
| Which action each reason earns (and the order) | `Policy.preferred[reason]` |
| Whether to rank by economics | `Policy.rank_by = "expected_value"` |
| How sure the model must be to act at all | `Policy.confidence_floor` |
| A stricter bar for expensive actions | `Scoring.min_confidence_by_type` |
| The cost/value/effectiveness numbers | `Scoring.type_cost` / `save_prior` / `type_effectiveness` / `value_horizon_months` |
| Which reasons a discount is legitimate for | `Policy.discount_reasons` |
| Which reasons to let go cleanly | `Policy.let_go_reasons` |
| Fine-grained eligibility (tenure, mrr, signals) | `Intervention.eligible_when` |

Omit any of it and you inherit the suggested SaaS defaults. See
[GUIDE.md §4](GUIDE.md#4-onboarding-a-company-the-config-model) for the config model.

---

## 7. Roadmap — where the framework should go next

Shipped: **EV scoring (#1)** and the **declared decision trace (#5)**, plus cost-tiered
confidence thresholds. Still suggested, not yet built:

- **Corroboration signal.** Check the model's `confidence` against the behavioral data it
  was given (`never_activated` + `activated:true` = contradiction) and discount confidence
  when the story and the data disagree. Declares a `corroborated` flag; guards the
  confident-and-wrong case.
- **`decline_to_spend` as a positive decision.** Today a `null` intervention can mean
  "below floor," "no match," or "EV ≤ 0." Separate *chose not to spend* (economics) from
  *nothing configured* (a config gap).
- **Recalibrate from the data asset.** A job over `runs/sessions.jsonl` that re-fits
  `save_prior` / `type_effectiveness` from realized saves — closing the loop the logs open.
- **Holdout arm.** Randomly withhold/vary offers for a small %, declare the arm on each
  outcome, and measure *incremental* saves — so offer efficiency is a number, not a claim.
- **Narration (safe LLM use).** An optional model call that *explains* an already-made
  decision in plain language — never one that makes it.
