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
    // The SDK shows the authorized offer IN the chat. You react to the user's choice:
    onAccept: (outcome) => {
      // They took the save. Apply it yourself — Offboard never calls Stripe.
      //   outcome.intervention.id  -> the authorized action to apply
      //   outcome.reason           -> the REAL reason (e.g. "price_value_mismatch")
      //   outcome.mode             -> "act" (auto-apply) | "suggest" (maybe human-approve)
      applyOffer(outcome);
    },
    onCancel: (outcome) => {
      // Declined the offer, none was authorized, or the escape hatch was tapped.
      completeCancellation();
    },
    // Optional analytics hook, fires when the interview concludes (before the offer step):
    onResolved: (outcome) => track(outcome),
  });
});
```

### React

```jsx
import { useOffboardCancelFlow } from "offboard/react";

function CancelButton({ user }) {
  const cancel = useOffboardCancelFlow({
    publicKey: "pk_live_...",          // or call Offboard.init() once elsewhere
    userId: user.id,
    identityToken: user.offboardToken, // see "Identity verification" below
    onAccept: (o) => applyOffer(o),
    onCancel: () => completeCancellation(),
  });
  return <button onClick={cancel}>Cancel subscription</button>;
}
```

`react` is an optional peer dependency — the core `offboard` import stays framework-free.

## Identity verification (required for paid offers)

The cancel flow runs in the browser, so **anything the browser sends can be forged**. If a
user opens devtools and posts `mrr: 99999`, they must not be able to unlock your richest save
offer. So when your Offboard key is configured with a **signing secret**, the engine ignores
the raw `context` economics and authorizes only off a token your **backend** signs.

1. Your server signs the user's real economics with your secret (never expose the secret to
   the browser) and hands the token to your frontend.
2. Pass it as `identityToken`. The engine verifies the HMAC and prices the save off the
   signed claims.

The token is `base64url(payload) + "." + base64url(HMAC_SHA256(secret, payload))`, where
`payload` is JSON of the user fields plus `iat` (issued-at, unix seconds; tokens expire after
10 minutes). Any language can mint it; the canonical implementation is
`api/identity.py::sign_identity` in the engine repo. Example payload:

```json
{ "user_id": "user_123", "plan": "Growth", "mrr": 199, "tenure_days": 210,
  "activated": true, "logins_last_30d": 27, "signals": { "seats_used": 7 }, "iat": 1736380800 }
```

Keys **without** a signing secret (local dev / the demo) trust the request body as-is — never
ship a production cancel flow on an unsecured key.

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
