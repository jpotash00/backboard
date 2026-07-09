# Offboard demo

A mock billing page that runs the **real SDK against the real engine** — the full loop:
cancel click → in-flow interview → diagnosis → authorized offer. It toggles between two
completely different products (a SaaS analytics tool and a meditation app) to show the
same pipeline is entirely config-driven.

## Run it

Three terminals (from the repo root):

```bash
# 1. Build the SDK (the demo imports sdk/dist/index.js)
cd sdk && npm install && npm run build && cd ..

# 2. Start the engine with BOTH demo customers loaded
export ANTHROPIC_API_KEY=sk-ant-...
OFFBOARD_CONFIG_DIR=configs python -m api.app        # serves on :8000

# 3. Serve the static demo (any static server; must be http, not file://)
python -m http.server 5500
```

Then open **http://localhost:5500/demo/**.

## What to try

- **Acme → "Signed up, never got going"** → open the interview with *"too expensive."*
  Watch it diagnose `never_activated` and offer a **setup call**, not a discount — the
  whole thesis in one click.
- **Acme → "Daily power user"** → same *"too expensive"* opener, but here it diagnoses
  `price_value_mismatch` and *does* authorize the discount. Same words, opposite call.
- **Zen → "Never built the habit"** → *"too expensive"* again, on a totally different
  product with its own taxonomy (`habit_never_formed`) → a guided-start offer.
- Tap **Just cancel** anytime — the escape hatch is always there, and it ends cleanly
  with no diagnosis.

The keys `pk_demo_acme` / `pk_demo_zen` map to `configs/acme.json` / `configs/zen.json`.
