# Offboard

An AI exit interview that lives **inside the cancel flow**, diagnoses why someone is
*actually* leaving (not the cover story they open with), and lets deterministic policy
authorize the right intervention from the customer's own pre-approved menu.

> **The claim being tested:** *"Too expensive" is the great lie of churn.* A person who
> says it but never activated has a value problem in a price costume — and a discount is
> the wrong medicine. The product is the gap between the **cover story** and the **reason**.

## Status: Milestone 1 (the eval)

Per the build brief, nothing downstream (API, SDK, dashboard) gets built until the core
claim is proven. **Milestone 1 is a blind eval** that answers, in about an hour: *can an
LLM find a churner's real reason when their opening line is a cover story?*

```
engine/                 the durable spine — reused unchanged by M2 (API) and M3 (SDK)
  taxonomy.py           the fixed churn taxonomy + data contracts
  interviewer.py        free-form probing, ≤3 turns, emits a structured diagnosis
  policy.py             LLM diagnoses, POLICY authorizes — the whole safety story
eval/                   Milestone 1
  configs.py            the test customer (Acme Analytics) config
  personas.py           10 synthetic churners, each an LLM roleplay w/ a hidden reason
  scoring.py            the 5 scores + the dropdown baseline (pure, testable)
  run_eval.py           the blind harness — run this
api/                    Milestone 2 — the FastAPI session engine
  schemas.py            request/response wire models (mirror the SDK types)
  registry.py           publishable-key -> customer ProductConfig
  store.py              in-memory session store (Redis-shaped interface)
  service.py            interviewer + policy orchestration (framework-agnostic)
  transcripts.py        JSONL logging of every completed session — the data asset
  app.py                the FastAPI app: /health, /sessions, /sessions/:id/turn
tests/                  pytest — spine invariants + API flow, no live API key needed
sdk/                    the web SDK (npm package `offboard`) — see sdk/README.md
```

The **npm package** (`sdk/`) is the drop-in cancel-flow SDK (Milestone 3). Its public
API and the wire contract are done and type-check today; point its `apiBaseUrl` at the
Milestone 2 server (below) and it runs end-to-end. It shares the exact `Outcome` shape
defined in `engine/taxonomy.py`.

## Run the eval

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # then add your ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...

python -m eval.run_eval     # prints full transcripts + scores, logs to runs/
```

## Run the API (Milestone 2)

```bash
pip install -e ".[api]"       # or ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
python -m api.app             # serves on :8000 (PORT env to change)
```

Then, with the demo customer key (`pk_demo_acme`):

```bash
# open a session
curl -s localhost:8000/sessions -H "Authorization: Bearer pk_demo_acme" \
  -H 'content-type: application/json' \
  -d '{"user_id":"u1","plan":"Starter","mrr":49,"tenure_days":210,
       "logins_last_30d":27,"activated":true,
       "usage_summary":"Daily active; constantly hitting the event cap."}'
# -> { "session_id": "...", "message": "..." }

# answer a turn (repeat until {"done": true, "outcome": {...}})
curl -s localhost:8000/sessions/<session_id>/turn -H "Authorization: Bearer pk_demo_acme" \
  -H 'content-type: application/json' -d '{"user_message":"it is too expensive"}'
```

Every completed session is appended to `runs/sessions.jsonl` (transcript + outcome).

## Try it in a browser (the full loop)

A clickable mock billing page that runs the real SDK against the real engine —
cancel → in-flow interview → diagnosis → authorized offer — and toggles between two
completely different products (SaaS analytics and a meditation app) to show the pipeline
is entirely config-driven. See **[demo/README.md](demo/README.md)** to run it. Start the
API with `OFFBOARD_CONFIG_DIR=configs` so both demo customers (`pk_demo_acme`,
`pk_demo_zen`) load.

## Run the tests (no API key required)

```bash
pytest
```

These lock the load-bearing invariants — e.g. `never_activated` can **never** receive a
discount, `value_ended` is let go with a pause, the confidence floor defers to the
caller's generic flow, and personas 1 vs 2 (same cover story, opposite truth) split.

## What "passing" means

- **Cover-story penetration ≥ 70%** — accuracy on the personas where a naive dropdown
  that believed the stated reason would be *wrong*. This is the real metric.
- **Personas 1 vs 2 split correctly** — identical cover story (`too_expensive`),
  opposite diagnosis (`never_activated` vs `price_value_mismatch`), opposite correct
  action. If these don't split, the system doesn't work.

Below the bar, iterate the interviewer prompt in `engine/interviewer.py` before building
anything else. **Read the transcripts** — where it got stuck is the product spec.

## Configuration

| Env var | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required to run the eval |
| `CHURN_MODEL` | `claude-sonnet-5` | the interviewer model |
| `PERSONA_MODEL` | `claude-sonnet-5` | the roleplay-churner model |

## Integrating (SDK + API + per-company config)

For how an engineer calls the SDK, runs the API, onboards a company via a
`ProductConfig`, and how the whole thing is designed end to end, see
**[docs/GUIDE.md](docs/GUIDE.md)**.

For how the decision engine authorizes offers (LLM diagnoses, policy authorizes; the
declared decision trace; EV scoring), see **[docs/DECISIONING.md](docs/DECISIONING.md)**.
For the randomized **holdout experiment** that measures the *causal* incremental save —
so offer efficiency is a number, not a claim — see **[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)**.
