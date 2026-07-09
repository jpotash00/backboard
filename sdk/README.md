# offboard (web SDK)

Drop-in cancel-flow SDK. Renders the exit-interview modal, runs the ≤3-question
interview against the Offboard engine, and hands your app a structured `Outcome`.

> **Status:** the package is complete and type-checks; it talks to the Offboard
> session API (`POST /sessions`, `POST /sessions/:id/turn`). Those endpoints ship in
> **Milestone 2**, so end-to-end runs go live once the engine API is deployed. The
> public API, wire contract, and UI are stable now.

## Install

```bash
npm install offboard
```

## Use

```js
import Offboard from "offboard";

// Once, at startup.
Offboard.init({ publicKey: "pk_live_..." });

// When the user clicks "Cancel subscription".
cancelButton.addEventListener("click", () => {
  Offboard.showCancelFlow({
    userId: "user_123",
    // Optional behavioral context — the richer this is, the sharper the diagnosis.
    context: {
      plan: "Starter",
      mrr: 49,
      tenure_days: 210,
      logins_last_30d: 27,
      activated: true,
      usage_summary: "Daily active; repeatedly hitting the event cap.",
    },
    onResolved: (outcome) => {
      // outcome.reason        -> the REAL reason (e.g. "price_value_mismatch")
      // outcome.cover_story   -> what they said first (e.g. "too_expensive")
      // outcome.intervention_id -> the authorized action, or null (fall back)
      // outcome.confidence    -> below 0.6, intervention_id is null by design
      applyIntervention(outcome);
    },
    onJustCancel: () => {
      // The always-visible escape hatch was tapped — cancel cleanly.
      completeCancellation();
    },
  });
});
```

## Design guarantees the SDK enforces

- **The "Just cancel" escape hatch is always rendered** (hard constraint #3). Someone
  who chooses to answer is telling the truth; someone cornered types anything to escape.
- **The conversation is bounded** — the engine stops at 3 questions; the modal closes on
  the server's `done` signal.
- **The model never authorizes anything.** `intervention_id` is chosen by deterministic
  server-side policy; the SDK just relays it.
- **Transport failures never trap the user** — on error the modal degrades to letting
  them cancel, it does not loop.

## Develop

```bash
npm install
npm run build       # tsc -> dist/
npm run typecheck   # strict, no emit
```

The wire types in `src/types.ts` mirror `engine/taxonomy.py`. Keep them in lockstep —
that shared `Outcome` shape is the contract between this SDK and the engine.
