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
tests/                  pytest — spine invariants, no API needed
sdk/                    the web SDK (npm package `offboard`) — see sdk/README.md
```

The **npm package** (`sdk/`) is the drop-in cancel-flow SDK (Milestone 3). Its public
API and the wire contract are done and type-check today; it goes end-to-end once the
Milestone 2 session API is deployed. It shares the exact `Outcome` shape defined in
`engine/taxonomy.py`.

## Run the eval

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # then add your ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...

python -m eval.run_eval     # prints full transcripts + scores, logs to runs/
```

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

## Known follow-up (deferred to M2)

`engine/policy.py` evaluates the `eligible_when` rule via a sandboxed `eval()` — kept
verbatim from the reference implementation for M1. Replace it with a real expression
parser before it processes untrusted customer config in production.
