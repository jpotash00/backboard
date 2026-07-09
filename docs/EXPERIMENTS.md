# Offboard — The Holdout Experiment (causal measurement)

The engine prices every save with two hand-set numbers — `save_prior[reason]` and
`type_effectiveness[type]` (see [DECISIONING.md §3](DECISIONING.md#3-the-suggested-economics-policyscoring)).
They start as opinionated guesses. This document is how you turn them into **measured
truth** without fooling yourself.

> **The one-line reason this exists.** `accepted / offered` — the number
> [`learning.recalibrate`](../learning/recalibrate.py) fits from — is not a save rate. It
> counts **always-stayers** (users who'd have kept the subscription with no offer) as saves,
> and it can't see **post-accept churn** (accepted the discount, cancelled next month). Only
> a randomized holdout removes both. Accept rate can rank your *worst* offer as your best.

---

## 1. The idea in one picture

```
                          ┌── treatment (offer shown)     ── retention R_treatment ─┐
  user hits cancel flow ──┤                                                          ├─► lift = R_treatment − R_control
                          └── control  (offer WITHHELD)   ── retention R_control  ──┘         = the CAUSAL save_prior × effectiveness
```

A fraction of sessions are randomly assigned to a **control** arm: fully interviewed and
diagnosed (you keep the churn reason), but shown **no offer** — they fall through to your
normal cancel flow. Compare the two arms' downstream retention at a fixed horizon and the
difference is the *incremental* save the offer actually caused. That difference is exactly
what `save_prior × effectiveness` is supposed to estimate — now with a number behind it.

**Why always-stayers cancel out:** they retain in *both* arms, so they add equally to
`R_treatment` and `R_control` and vanish from the difference. **Why post-accept churn is
caught:** retention is read at the horizon (e.g. 30 days out), not at the moment of accept.

---

## 2. Turning it on

Off by default — a customer opts in when they're ready to trade a slice of saves for a
causal read. One config block ([`engine/taxonomy.py`](../engine/taxonomy.py) → `Experiment`):

```jsonc
"experiment": {
  "holdout_fraction": 0.1,     // 10% of sessions see no offer (the control arm)
  "experiment_id": "default",  // change this to START A CLEAN EXPERIMENT (re-draws everyone)
  "horizon_days": 30           // retention is measured at/after this many days
}
```

| Knob | What it does | Notes |
|---|---|---|
| `holdout_fraction` | Share of sessions held out as control | `0.0` = off (everyone treated, no save withheld). `1.0` = everyone held out. |
| `experiment_id` | Namespaces the randomization | Changing it re-draws **every** assignment, so you don't silently mix two designs' data into one dataset. |
| `horizon_days` | When retention is judged | Larger = truer (less post-accept churn missed) but slower to read out. |

---

## 3. How assignment works ([`api/experiment.py`](../api/experiment.py))

Assignment is a **deterministic hash** of `(experiment_id, customer_id, user_id)` — not an
RNG. Two reasons:

1. **Stability.** A retried `POST /sessions`, or the same user returning, lands in the same
   arm. A user can't be held out today and treated tomorrow — that would contaminate the read.
2. **Auditability.** The arm is a pure function of its inputs, reproducible offline from the
   logs. No hidden random state to reconcile.

The hash maps uniformly to `[0, 1)`; the bottom `holdout_fraction` is control. Uniformity
means the control share converges to `holdout_fraction` over many users.

```python
from api.experiment import assign
assign("default", "acme", "user-42", 0.1)   # -> "treatment" | "control", stable
```

---

## 4. What each arm experiences

Both arms are **diagnosed** — the interview runs, the reason is resolved, `policy.decide`
authorizes the intervention. The arm only decides whether that offer is **served**.

| | Treatment | Control |
|---|---|---|
| Interview + diagnosis | ✅ | ✅ |
| Offer computed (`intended_intervention_id`) | ✅ | ✅ (the **counterfactual** — never shown) |
| Offer served in the response | ✅ | ❌ (`intervention: null` → host's normal cancel flow) |
| `offered` in the log | `true` | `false` |

Control keeps the full `decision_trace`/`economics` for audit — only the servable
`intervention_id` is cleared (in `service._serve`), which is what gates rendering in the SDK.
So you always know *what you would have offered* a held-out user, which is the cell key that
makes the two arms comparable.

---

## 5. Closing the loop: `POST /outcomes`

Sessions are ephemeral (30-min TTL, in-memory), so downstream retention can't live on the
session. Your billing backend reports it later:

```http
POST /outcomes
Authorization: Bearer pk_...
{ "user_id": "user-42", "active": false, "observed_at": "2026-03-01T00:00:00+00:00" }
```

- `user_id` must match the id used at session start (that's the join key).
- `active` = still subscribed. `observed_at` = when that was true (you supply it, so a batch
  backfill reports real observation times, not ingestion time).
- Append-only and idempotent-friendly: send as many checks over time as you like; the readout
  takes the observation at/after the horizon. Reporting for a user you never offered to is
  harmless — it simply won't join.

Records land in `runs/outcomes.jsonl`, joined to `runs/resolutions.jsonl` by
`(customer_id, user_id)`.

---

## 6. Reading the result ([`learning.experiment`](../learning/experiment.py))

```bash
python -m learning.experiment runs/resolutions.jsonl runs/outcomes.jsonl 30
```

```
CAUSAL LIFT READOUT (holdout vs treatment)
============================================================
  joined=6000  pending=0  unmatched=0  skipped=0

  reason              offer         R_ctl  R_trt    lift            95% CI  accept  note
  never_activated     onboarding     0.18   0.20   +0.02     [-0.01,+0.05]    0.75  not significant (CI spans 0)
  price_value_mismatch discount      0.32   0.71   +0.39     [+0.36,+0.43]    0.63  significant
```

Read the two rows against each other — this is the whole point:

- **`price_value_mismatch → discount`**: accept rate 0.63, but the offer *causes* a **+0.39**
  retention lift (CI excludes 0). A real save.
- **`never_activated → onboarding`**: accept rate **0.75 — the highest** — yet lift is **+0.02,
  not significant**. Its accepters were always-stayers. Accept rate would tell you to fund
  this offer; the holdout shows it saves no one.

Columns:

| Column | Meaning |
|---|---|
| `R_ctl` / `R_trt` | Retention in control / treatment at the horizon |
| `lift` | `R_trt − R_control` — the causal `save_prior × effectiveness` |
| `95% CI` | Newcombe interval on the lift; **excludes 0 ⇒ significant** |
| `accept` | The observational `accepted/offered` — shown *only* to expose the gap |
| `note` | `NO CONTROL` (raise the holdout) · `underpowered` · `significant` · `not significant` |

The join is honest about what it can't score yet: `pending` (session reached but no outcome
past the horizon), `unmatched` (no outcome reported at all), `skipped` (malformed rows).
Nothing is guessed.

**Net value.** `CellResult.net_value(value_horizon_months, cost)` = `lift × LTV − cost ×
accept_rate` — expected dollars per treated user, net of the margin actually given away (you
only pay on accepted offers). It goes **negative** when a costly offer buys no lift — the
thing accept rate structurally cannot tell you.

### The statistics ([`learning/stats.py`](../learning/stats.py), dependency-free)

- **Wilson interval** on each arm's retention — stays in `[0,1]` at small n / extreme rates.
- **Newcombe method 10** for the difference CI — robust where a normal-approx difference
  interval misbehaves.
- **Pooled two-proportion z-test** for the p-value.

> **Significance is per-experiment and probabilistic.** A 95% test false-positives ~5% of
> the time by construction — don't over-read a single marginal cell. The validation suite
> ([`tests/test_causal_readout.py`](../tests/test_causal_readout.py)) asserts the *right*
> property: over many null experiments the lift centers on zero and false positives stay near
> 5%, while accept rate reports a fake save every time.

---

## 7. How much holdout, for how long

The cost of the experiment is the saves you forgo in the control arm; the benefit is a
trustworthy number. Rules of thumb:

- **Start small and clean:** `holdout_fraction: 0.1`, a single fixed holdout. Don't reach for
  bandits yet — they make the statistics much harder to interpret while you're still learning.
- **Power before precision:** a cell needs enough users *per arm* (the report flags
  `underpowered` below ~30/arm) before its lift is worth acting on. Rare reasons take longer.
- **Horizon is a tradeoff:** longer horizons catch more post-accept churn but delay every
  readout. Pick the shortest horizon past which churn is rare for your product.
- **Once baselines are established** you can shrink or rotate the holdout to reduce the
  forgone-save cost, since the baseline per reason changes slowly.

---

## 8. Feeding it back (deliberately not automated)

The causal lift **supersedes** `learning.recalibrate`'s observational estimate once the
holdout matures — `lift(reason, type)` *is* the causal `save_prior[reason] ×
effectiveness[type]`. Writing it back into `Scoring` (the equivalent of
`learning.apply_proposal`, human-approved) is **not yet built**, on purpose: closing that loop
before you've watched a real readout mature would be exactly the premature automation the
[decisioning framework](DECISIONING.md#1-two-decisions--only-one-is-an-llm) argues against.
Measure first, watch a few cells reach significance, then wire the proposal.

Until a holdout is running, `learning.recalibrate` remains the best available estimate — its
report headlines the selection-bias caveat and now points here.

---

## 9. File map

| File | Role |
|---|---|
| [`engine/taxonomy.py`](../engine/taxonomy.py) → `Experiment` | The per-customer config (fraction, id, horizon) |
| [`api/experiment.py`](../api/experiment.py) | Deterministic arm assignment |
| [`api/service.py`](../api/service.py) → `_serve` | Withholds the offer for control; logs the counterfactual |
| [`api/transcripts.py`](../api/transcripts.py) | Logs `arm` + `intended_intervention_type`; `log_outcome` |
| `POST /outcomes` ([`api/app.py`](../api/app.py)) | Ingests downstream retention |
| [`learning/experiment.py`](../learning/experiment.py) | Join, per-cell lift, CIs, net value, report |
| [`learning/stats.py`](../learning/stats.py) | Wilson / Newcombe / two-proportion, no deps |
| [`tests/test_causal_readout.py`](../tests/test_causal_readout.py) | Proves it recovers a planted lift and isn't fooled by accept rate |
