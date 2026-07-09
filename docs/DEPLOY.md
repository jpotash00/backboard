# Deploying the Offboard Engine API

This is the ops guide for running `api/` in production. For integrating the SDK and authoring
per-company configs, see [GUIDE.md](GUIDE.md). For the decision/economics model, see
[DECISIONING.md](DECISIONING.md); for the holdout, [EXPERIMENTS.md](EXPERIMENTS.md).

The service is a stateless-ish FastAPI app. The only mutable state is (1) in-flight cancel
sessions and (2) the append-only data asset. Both have a production story below — get those two
right and the rest is a standard container deploy.

---

## TL;DR checklist

- [ ] `ANTHROPIC_API_KEY` set as a secret (not baked into the image).
- [ ] **Every production customer has a `signing_secret`** — otherwise the browser can spoof its
      own economics (`mrr`, `plan`) and unlock a paid offer. This is the one silent security
      footgun. See [Security](#security).
- [ ] Session store: either **one always-on instance** (in-memory) **or** set `OFFBOARD_REDIS_URL`
      for multiple instances / zero-downtime deploys. See [Session state](#session-state).
- [ ] `OFFBOARD_RUNS_DIR` points at a **mounted persistent volume** — otherwise the data asset
      (resolutions/outcomes, i.e. the whole flywheel) is wiped on every redeploy. See
      [The data asset](#the-data-asset).
- [ ] `OFFBOARD_CONFIG_DIR` points at your stored customer configs (JSON). See
      [Provisioning customers](#provisioning-customers).
- [ ] TLS terminated in front (identity tokens and economics travel over the wire).
- [ ] `/health` wired to the platform's health check.

---

## Two deployment topologies

Pick based on volume and your tolerance for dropped sessions during a deploy.

### A. Single instance (simplest — good for a pilot)

One always-on container, in-memory session store. Zero extra infra.

**Constraints, by design:**
- You can run **exactly one instance**. Two instances behind a load balancer will 404 roughly
  half of all `/turn` calls, because a session lives in the memory of the one process that
  started it.
- A restart or redeploy **drops every in-flight cancel session** mid-interview. Users mid-flow
  get a clean error and fall through to your normal cancel path — not catastrophic, but not
  zero-downtime.

This is fine for a controlled pilot with modest concurrent cancel volume. Mount a volume for
the data asset (below) and you're production-shippable for that scope.

### B. Multiple instances + Redis (real production)

Set `OFFBOARD_REDIS_URL` and the app uses `RedisSessionStore`: sessions are persisted as JSON
snapshots under a TTL and rehydrated on each request, so **any instance can resume a session
another started**, and a redeploy doesn't drop in-flight interviews. Horizontal scaling and
rolling deploys both work.

```
OFFBOARD_REDIS_URL=redis://:password@my-redis-host:6379/0
```

Install the optional backend: the image built from the provided `Dockerfile` already includes
it (`pip install .[api,redis]`). If you build your own, install the `redis` extra.

Nothing else changes — the store is selected automatically from the env var, and both backends
satisfy the same `create/get/save` contract.

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | — | Model access for the interviewer. Set as a secret. |
| `OFFBOARD_CONFIG_DIR` | prod | (demo customer) | Directory of per-customer JSON configs. Without it, only the built-in `pk_demo_acme` demo customer exists. |
| `OFFBOARD_RUNS_DIR` | prod | `runs` | Where the append-only JSONL data asset is written. **Point at a mounted volume.** |
| `OFFBOARD_REDIS_URL` | topology B | (unset → in-memory) | Redis connection URL. Set it to run multiple instances. |
| `PORT` | no | `8000` | Bind port (most hosts inject this). |
| `CHURN_MODEL` | no | `claude-sonnet-5` | Interviewer model id. |
| `OFFBOARD_MODEL_TIMEOUT` | no | `30` | Per-model-call timeout (seconds) before a clean error. |

---

## Session state

The live interview holds a model client + message history, which isn't serializable. The
serialization seam is `SessionState.snapshot()` / `store.restore()` — snapshot captures the
transcript, message history, and turn count; restore rebuilds the interviewer, re-attaching the
customer's config (by `customer_id`, via the registry) and a fresh model client.

- **In-memory** (`SessionStore`): dict + sliding TTL eviction (default 30 min). Abandoned flows
  are evicted, not pinned.
- **Redis** (`RedisSessionStore`): one key per session with a TTL; the TTL slides forward on
  each read. Eviction is Redis-native. A session whose customer was de-provisioned mid-flow
  reads back as "not found" (safe).

TTL default is 30 minutes — long enough for a real interview, short enough to bound memory/keys.

## The data asset

`transcripts` / `resolutions` / `outcomes` are append-only JSONL under `OFFBOARD_RUNS_DIR`. This
is the compounding asset: `resolutions.jsonl` + `outcomes.jsonl` are exactly what
`learning.recalibrate` and the causal holdout readout consume.

On an ephemeral container filesystem (Render, Fly, Railway, Fargate, …) this directory is
**wiped on every redeploy** unless it's a mounted volume. So:

- Mount a persistent volume and set `OFFBOARD_RUNS_DIR` to a path on it (the `Dockerfile`
  defaults to `/data/runs` and declares `VOLUME ["/data"]`).
- With **topology B (multiple instances)** a single local volume isn't shared across instances.
  Either mount shared network storage, or (recommended at scale) ship these appends to object
  storage / a database. The writer (`api/transcripts.py`) is a small, single-responsibility
  seam — swapping the sink is a contained change. Until then, keep the log-writing instance
  singular or accept per-instance shards you concatenate offline before a readout.

Losing this data doesn't break the live flow — offers still fire — but it silently resets the
flywheel. Treat the volume as required for anything past a throwaway demo.

## Provisioning customers

Set `OFFBOARD_CONFIG_DIR` to a directory of JSON configs (one per customer); the registry loads
them at startup (`registry_from_dir`). See [GUIDE.md §4](GUIDE.md) for the config schema and
`configs/acme.json` / `configs/zen.json` for worked examples. Each customer needs:

- a **publishable key** (`pk_...`) — public by design; it identifies, it does not secure;
- a **`signing_secret`** — see below;
- optional **`allowed_origins`** — browser origins permitted to use the key (403 otherwise).

## Security

The publishable key is public (it ships in the browser), so it is **not** the security boundary.
Three controls carry the weight, and one of them is opt-in per customer:

1. **Signed identity (the footgun).** If a customer has a `signing_secret`, only economics from a
   server-signed identity token are trusted; the raw request body's `mrr`/`plan`/etc. are
   ignored. If a customer is provisioned **without** a `signing_secret`, the API falls back to
   trusting the request body — meaning a user can edit `mrr=99999` in the browser and unlock a
   discount they shouldn't get. **Every production customer must have a `signing_secret`.** The
   customer's backend signs a short-lived token (see `api/identity.py`) and the SDK passes it as
   `identityToken`.
2. **Rate limiting.** Per-key + per-IP fixed-window limits on the model-cost endpoints, already
   enforced (`api/ratelimit.py`).
3. **Origin allowlist.** Per-customer `allowed_origins`, enforced at auth (403). Defense in depth
   against a lifted key driven from an attacker's page.

Also: terminate **TLS** in front (tokens + economics in transit), and keep `ANTHROPIC_API_KEY`
and every `signing_secret` in your platform's secret store, never in the image or in git.

---

## Build & run

```bash
# Local (single instance, in-memory, data in ./runs)
pip install ".[api]"
OFFBOARD_CONFIG_DIR=configs offboard-api        # serves on :8000

# Container (the provided Dockerfile; mount a volume at /data)
docker build -t offboard-api .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e OFFBOARD_CONFIG_DIR=/config \
  -e OFFBOARD_REDIS_URL=redis://redis:6379/0 \
  -v offboard-data:/data \
  -v $PWD/configs:/config:ro \
  offboard-api
```

Health check: `GET /health` → `{"status":"ok"}` (needs no auth, makes no model call).

## Go-live runbook (first production customer)

The system has two publish surfaces — **you host the engine API**; **the customer installs the
npm SDK** that calls it. Do them in this order; the SDK is inert until the API is live.

### 0. Decide, once
- A domain for the API, e.g. `api.offboard.dev` (must match the SDK's base URL — see step 3).
- An `ANTHROPIC_API_KEY`.
- An npm account, and the package name (`offboard`, or `@your-org/offboard` if taken).

### 1. Provision the customer on disk
Write a **tiny spec** — only the four product facts + the authorized offer menu; taxonomy,
policy, and economics all come from validated defaults (template: `docs/customer-spec.example.json`):

```jsonc
{
  "customer_id": "acme",
  "public_key": "pk_live_acme_9f3k…",           // public; ships in their browser
  "allowed_origins": ["https://app.acme.com"],
  "product": { "product_name": "…", "product_context": "…",
               "activation_definition": "…", "pricing_summary": "…" },
  "offers": [ { "type": "discount", "description": "50% off for 3 months" },
              { "type": "pause",    "description": "Pause billing up to 3 months" } ]
}
```

Then run the provisioner — it **mints the `signing_secret`** (so you can't ship a
trust-the-browser tenant), validates the config, and writes `<customer_id>.json` into the dir:

```bash
python -m onboarding.provision acme.spec.json --out /path/to/OFFBOARD_CONFIG_DIR
# prints the signing_secret ONCE — store it; the customer's backend signs identity tokens with it
```

The offer menu is declared, not inferred. For messy free-text pricing, draft a config with the
`onboarding/` proposer first, review it, then list the offers in the spec. Keep these files
private — they carry the signing secret — and never overwrite one in place (that rotates the
secret; the provisioner refuses to clobber for exactly this reason).

### 2. Deploy the engine API
Any container host works (the image is standard). Fly.io, concretely:

```bash
fly launch --no-deploy                       # detects the Dockerfile, writes fly.toml
fly volumes create offboard_data --size 1    # persistent disk for /data (runs + configs)
fly redis create                             # managed Redis -> copy the redis:// URL
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-… \
  OFFBOARD_REDIS_URL=redis://…@…:6379 \
  OFFBOARD_CONFIG_DIR=/data/configs \
  OFFBOARD_RUNS_DIR=/data/runs
# mount the volume at /data in fly.toml, then put the customer file on it:
fly deploy
fly ssh console -C "mkdir -p /data/configs"
cat configs/acme.json | fly ssh console -C "tee /data/configs/acme.json >/dev/null"
fly deploy                                   # restart so the registry loads the config
fly certs create api.offboard.dev            # + point the DNS record it prints
curl https://api.offboard.dev/health         # -> {"status":"ok"}
```

(Render/Railway are the same shape: Docker service + a persistent disk + a managed Redis +
the four env vars. Put the config files on the disk, not in the image — they hold secrets.)

### 3. Point the SDK at prod, then publish to npm
If your domain is not `api.offboard.dev`, set `DEFAULT_API_BASE_URL` in `sdk/src/index.ts` to
your domain (no `/v1` unless you also set `OFFBOARD_ROOT_PATH`). Then:

```bash
cd sdk
npm login && npm whoami
npm pack --dry-run          # confirm only dist/ + README ship — never src/ or the engine
npm version minor
npm publish                 # prepack builds dist/; access:public is configured
```

### 4. Hand the customer their integration
```ts
import Offboard from "offboard";
Offboard.init({ publicKey: "pk_live_acme_9f3k…" });     // the key from step 1

// on their "Cancel" click:
Offboard.showCancelFlow({
  userId: currentUser.id,
  identityToken: await fetch("/api/offboard-token").then(r => r.text()), // minted on THEIR backend
  theme: { adoptHostTokens: true },
  onAccept: (o) => applyViaStripe(o.intervention!.id),   // Offboard never touches billing
  onCancel: () => finishCancellation(),
});
```
Their backend mints `identityToken` by HMAC-signing the user's economics with the shared
`signing_secret` (the scheme is `api/identity.py`; document it for them). Without it, a key that
has a signing secret rejects the session — which is the point.

### 5. Verify prod end-to-end
Run one real cancellation against the live URL and confirm a row lands in
`/data/runs/resolutions.jsonl`. Later, have their billing system POST retention truth to
`/outcomes`; then `python -m learning.recalibrate` and the holdout readout turn that into
measured lift.

## What is NOT deployed here

The Python engine and the interviewer prompts are **server-side only** — they never ship to npm
or PyPI (that would leak the IP). The browser gets only the published npm SDK (`dist/` — see
[../sdk/PUBLISHING.md](../sdk/PUBLISHING.md)), which talks to this API over HTTPS.
