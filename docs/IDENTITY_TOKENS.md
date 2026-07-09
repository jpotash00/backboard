# Identity tokens — the customer integration spec

Hand this page to the customer's backend engineer. It is the one thing they must build on
their side. Everything else is `Offboard.init(...)` + `showCancelFlow(...)` (see
[GUIDE.md](GUIDE.md)).

## Why

The cancel flow runs in the user's browser, so anything the browser sends is
attacker-controlled. The fields that authorize a **paid** save offer — `mrr`, `plan`,
`tenure`, activation, behavioral `signals` — must therefore be vouched for by *your* backend,
not read from the request body. Otherwise a user opens devtools, sends `mrr: 99999`, and
unlocks your most generous offer.

So: your backend mints a short-lived, HMAC-signed token over the user's **verified** economics;
the SDK forwards it as `identityToken`; the engine verifies the signature and authorizes off
the token's claims, ignoring the raw body. Same pattern as Intercom identity verification.

**A customer provisioned with a `signing_secret` will *reject* any session without a valid
token.** That is the point — there is no trust-the-browser fallback in production.

## The secret

You were given a `signing_secret` (a 64-char hex string) when your customer was provisioned. It
is shown **once**. Keep it in your server-side secret store. It never touches the browser and
never leaves your backend. If it leaks, ask for a re-provision to rotate it.

## The token format

```
token = base64url(payload_json) + "." + base64url( HMAC_SHA256(secret, base64url(payload_json)) )
```

- `payload_json` is compact JSON (no spaces) with **sorted keys**, containing the user claims
  plus `iat` (issued-at, unix **seconds**).
- base64url is **unpadded** (strip `=`).
- The engine rejects a token older than **600 seconds** (10 min) or dated more than 60s in the
  future. So mint a fresh token per cancel-flow open — do not cache them.

### Claims to include

| Claim | Type | Meaning |
|---|---|---|
| `userId` | string | Must match the `userId` passed to `showCancelFlow` |
| `mrr` | number | The user's true monthly recurring revenue, in dollars |
| `plan` | string | Plan identifier (matches your policy rules) |
| `tenure_months` | number | How long they've been a paying customer |
| `activated` | boolean | Whether they hit your activation definition |
| `signals` | object | Optional derived behavioral signals (see GUIDE.md §5) |
| `iat` | number | Unix seconds — **added by the signer below**, don't set it yourself |

Send only what your policy actually uses; extra claims are ignored.

## Reference signer (Node / TypeScript, zero dependencies)

This is byte-for-byte compatible with the engine's `api/identity.py` (verified: a token minted
here is identical to one minted by `sign_identity`, nested `signals` included).

> ⚠️ Do **not** shortcut the canonical JSON with `JSON.stringify(payload, keys.sort())`. That
> replacer-array trick applies the key list as a whitelist at *every* nesting level, so it
> silently drops the contents of nested objects like `signals` (you'd ship `signals:{}`) and
> doesn't sort them. Use the recursive `canonical()` below, which matches Python's
> `json.dumps(sort_keys=True, separators=(",",":"))` exactly.

```ts
import { createHmac } from "node:crypto";

function b64url(buf: Buffer): string {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Canonical JSON: recursively sort object keys, compact separators. Must match the server. */
function canonical(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  const keys = Object.keys(v as object).sort();
  return "{" + keys.map(k => JSON.stringify(k) + ":" + canonical((v as any)[k])).join(",") + "}";
}

/** Mint an Offboard identity token. Call this on your backend, per cancel-flow open. */
export function signIdentity(secret: string, claims: Record<string, unknown>): string {
  const payload = { ...claims, iat: Math.floor(Date.now() / 1000) };
  const payloadB64 = b64url(Buffer.from(canonical(payload), "utf8"));
  const sig = b64url(createHmac("sha256", secret).update(payloadB64, "ascii").digest());
  return `${payloadB64}.${sig}`;
}
```

Expose it behind an authenticated route so only a logged-in user can mint their own token:

```ts
// e.g. GET /api/offboard-token  (user must be authenticated)
app.get("/api/offboard-token", requireAuth, (req, res) => {
  const u = req.user; // your verified record
  res.type("text/plain").send(signIdentity(process.env.OFFBOARD_SIGNING_SECRET!, {
    userId: u.id, mrr: u.mrr, plan: u.plan,
    tenure_months: u.tenureMonths, activated: u.activated,
  }));
});
```

### Python signer

If your backend is Python, you already have the canonical implementation — import it:

```python
from api.identity import sign_identity
import time
token = sign_identity(SIGNING_SECRET, {"userId": u.id, "mrr": u.mrr, "plan": u.plan}, int(time.time()))
```

## Wiring it in the browser

```ts
Offboard.showCancelFlow({
  userId: currentUser.id,
  identityToken: await fetch("/api/offboard-token").then(r => r.text()), // minted on YOUR backend
  onAccept: (o) => applyViaStripe(o.intervention!.id),   // Offboard never touches billing
  onCancel: () => finishCancellation(),
});
```

## Verifying it works

A wrong or missing token yields a clean auth error (the flow falls through to your normal cancel
path — users are never stuck). Test both:
- valid token → interview starts, and a paid offer is authorized where policy allows;
- tampered `mrr` in devtools → **no** effect (the body is ignored; only signed claims count).
