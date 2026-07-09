# Offboard

**Stop discounting your way through churn you don't understand.**

Offboard is an AI exit interview that lives **inside your cancel flow**. When a customer
clicks "cancel," it diagnoses why they're *actually* leaving — not the cover story they
open with — then lets deterministic, server-side policy authorize the right intervention
from *your* pre-approved menu.

## The problem

Every retention flow is built on a lie the customer told you. The cancel dropdown asks
"why are you leaving?", the customer picks **"too expensive"**, and you reflexively fire a
discount at them. But *"too expensive"* is the great cover story of churn. The person who
says it and never activated has a **value** problem wearing a **price** costume — and a
discount is the wrong medicine. You just paid to lose them slower.

The dropdown can't tell those people apart. A one-click reason menu has no way to
distinguish the churner you can save with a discount from the one for whom a discount is
money set on fire. So retention becomes a blunt, expensive guess.

## The idea

The product is the gap between the **cover story** and the **reason**.

```
cover_story   what they SAID first     e.g. "too_expensive"
reason        what's ACTUALLY true     e.g. "never_activated"
```

A short, in-flow conversation probes past the opening line to the real reason. Then the
safety split does the rest: **the model diagnoses, it never decides.** A deterministic
policy maps the diagnosed reason to an intervention drawn only from actions *you*
authorized — so a `never_activated` churner can never be handed a discount, no matter what
the model thinks. You get the intelligence of an LLM with the control of a rules engine.

## Why it wins

- **Right medicine, not reflexive discounts.** Interventions match the real reason, so you
  stop buying saves you'd have gotten free and stop discounting churn no discount can fix.
- **You stay in control.** Every action comes from your pre-approved menu; the model can't
  invent an offer or over-promise.
- **Config-driven, drop-in.** One npm package in the cancel flow; every product's reasons,
  offers, and policy are just config. The same engine runs a SaaS tool and a meditation app.
- **A causal number, not a claim.** A built-in randomized holdout measures the *incremental*
  save rate, so retention spend is something you can actually defend.

## Learn more

- **[docs/GUIDE.md](docs/GUIDE.md)** — integrate it: the SDK, the API, and onboarding a
  company via a `ProductConfig`, plus how the whole thing is designed.
- **[docs/DECISIONING.md](docs/DECISIONING.md)** — how offers get authorized: LLM diagnoses,
  policy authorizes, the declared decision trace, and EV scoring.
- **[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)** — the randomized holdout that turns "we save
  customers" into a measured, causal number.
- **[docs/BUILD.md](docs/BUILD.md)** — project status, repo layout, and how to run the eval,
  the API, the demo, and the tests.
