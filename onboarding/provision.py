"""Hand-provision one customer -- the no-UI onboarding path.

Turns a *tiny* human spec (the four product facts + the authorized offer menu) into a complete,
validated customer record and writes it into OFFBOARD_CONFIG_DIR, where the server's file-drop
loader (api/config_store.py) picks it up at boot. Everything the spec omits -- the churn
taxonomy, the policy, the economics, the experiment -- comes from validated defaults, so the
operator only states what is genuinely per-customer.

Two safety properties this enforces that hand-writing the JSON does not:
  - a `signing_secret` is ALWAYS minted (unless you pass one to reuse), so you can't accidentally
    ship a tenant in trust-the-browser mode -- the mrr-spoofing hole.
  - the config is validated before it's written, so a broken record fails HERE, at provision
    time, not at server boot or mid-interview.

The offer menu is declared, never inferred -- it's a margin decision, not something to guess
(the same stance the LLM proposer takes). For messy free-text pricing, use onboarding.propose to
draft a config first, review it, then hand the offers to this.

CLI:  python -m onboarding.provision spec.json [--out configs]
"""

import json
import secrets
from pathlib import Path
from typing import Optional

from engine import ProductConfig, config_to_dict

# Reuse the proposer's offer->Intervention normaliser (unique-id guarantee) so the two onboarding
# paths produce identical intervention shapes.
from .proposer import _interventions


class ProvisionError(Exception):
    """The spec is missing something required, or the resulting config is invalid."""


REQUIRED_PRODUCT_FIELDS = (
    "product_name", "product_context", "activation_definition", "pricing_summary",
)


def build_record(spec: dict, *, signing_secret: Optional[str] = None) -> dict:
    """Tiny spec -> full customer envelope (ready to write or POST /configs). Shape of `spec`:

        {
          "customer_id": "acme",
          "public_key": "pk_live_acme",
          "allowed_origins": ["https://app.acme.com"],   # optional
          "product": {
            "product_name": "Acme Analytics",
            "product_context": "one or two sentences on what it does and for whom",
            "activation_definition": "what 'activated' means here",
            "pricing_summary": "the plans and prices"
          },
          "offers": [                                      # the authorized menu (required)
            {"type": "discount", "description": "50% off for 3 months"},
            {"type": "pause",    "description": "Pause billing for up to 3 months"}
          ],
          "competitors": ["..."]                           # optional
        }

    Everything else -- reasons, policy, scoring, corroboration, experiment -- is left at the
    validated defaults. Returns the envelope; `config` is a JSON-safe ProductConfig dict.
    """
    for key in ("customer_id", "public_key"):
        if not spec.get(key):
            raise ProvisionError(f"spec is missing required '{key}'")
    product = spec.get("product") or {}
    missing = [f for f in REQUIRED_PRODUCT_FIELDS if not product.get(f)]
    if missing:
        raise ProvisionError(f"spec.product is missing: {', '.join(missing)}")

    interventions = _interventions(spec.get("offers", []))
    if not interventions:
        raise ProvisionError(
            "spec.offers is empty or unusable -- the offer menu is required (it can't be inferred)"
        )

    try:
        config = ProductConfig(
            product_name=product["product_name"],
            product_context=product["product_context"],
            activation_definition=product["activation_definition"],
            pricing_summary=product["pricing_summary"],
            competitors=list(spec.get("competitors", [])),
            interventions=interventions,
            # reasons / policy / scoring / experiment: validated defaults.
        ).validate()
    except ValueError as e:
        raise ProvisionError(f"config failed validation: {e}") from e

    return {
        "customer_id": str(spec["customer_id"]),
        "public_key": str(spec["public_key"]),
        "signing_secret": signing_secret or secrets.token_hex(32),
        "allowed_origins": list(spec.get("allowed_origins", ())),
        "config": config_to_dict(config),
    }


def write_customer_file(record: dict, config_dir: str) -> Path:
    """Write the envelope as <config_dir>/<customer_id>.json. Refuses to clobber an existing file
    -- overwriting would silently rotate that customer's signing_secret and break live sessions;
    delete it deliberately to re-provision."""
    directory = Path(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['customer_id']}.json"
    if path.exists():
        raise ProvisionError(
            f"{path} already exists; refusing to overwrite (that would rotate the signing_secret). "
            "Delete it first to re-provision."
        )
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def main() -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(
        description="Provision one Offboard customer from a tiny spec (no UI)."
    )
    ap.add_argument("spec", help="path to the spec JSON (see build_record's docstring)")
    ap.add_argument(
        "--out", default=os.getenv("OFFBOARD_CONFIG_DIR", "configs"),
        help="config directory to write into (default: $OFFBOARD_CONFIG_DIR or ./configs)",
    )
    ap.add_argument(
        "--signing-secret", default=None,
        help="reuse an existing secret instead of minting one (rare; e.g. re-provisioning)",
    )
    args = ap.parse_args()

    try:
        spec = json.loads(Path(args.spec).read_text())
        record = build_record(spec, signing_secret=args.signing_secret)
        path = write_customer_file(record, args.out)
    except (ProvisionError, json.JSONDecodeError, OSError) as e:
        print(f"provision failed: {e}")
        return 1

    print(f"wrote {path}")
    print(f"  customer_id : {record['customer_id']}")
    print(f"  public_key  : {record['public_key']}")
    print(f"  signing_secret (store this now -- shown once): {record['signing_secret']}")
    print(f"  offers      : {len(record['config']['interventions'])}  (taxonomy + policy = defaults)")
    print("Restart the API (or redeploy) to load the new customer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
