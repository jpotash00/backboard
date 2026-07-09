"""File-backed config store: load a directory of customer configs into the registry.

This is the production-shaped path the registry docstring promises -- each customer's
ProductConfig is a stored, versioned JSON record rather than in-code Python. Swap the
directory glob for a DB query and nothing else in the request path changes.

One file per customer:
  {"customer_id": "...", "public_key": "pk_...", "config": {<ProductConfig JSON>}}
"""

import json
from pathlib import Path
from typing import Union

from engine import config_from_dict

from .registry import Customer, CustomerRegistry


def load_customer_file(path: Union[str, Path]) -> Customer:
    """Parse one customer file. `config_from_dict` validates, so a broken config here
    raises at load with a clear message instead of failing mid-interview. The stored
    signing_secret is decrypted with the master key (api.crypto) -- an unmigrated plaintext
    secret passes through, an encrypted one with no/wrong key fails closed at load."""
    from .crypto import decrypt_secret

    data = json.loads(Path(path).read_text())
    return Customer(
        id=data["customer_id"],
        public_key=data["public_key"],
        config=config_from_dict(data["config"]),
        signing_secret=decrypt_secret(data.get("signing_secret")),
        allowed_origins=tuple(data.get("allowed_origins", ())),
    )


def registry_from_dir(config_dir: Union[str, Path]) -> CustomerRegistry:
    """Build a registry from every *.json in a directory. A present-but-broken file still fails
    fast (below); but an EMPTY or absent dir is a valid cold start -- a fresh deploy on a new
    volume has no configs yet -- so we boot with an empty registry rather than crash-looping the
    machine. Customers are then added at runtime via POST /configs (the no-restart provisioning
    path), or by dropping a file and restarting."""
    reg = CustomerRegistry()
    d = Path(config_dir)
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        print(
            f"WARNING: no *.json customer configs in {config_dir}; starting with an EMPTY "
            "registry. Provision via POST /configs (needs OFFBOARD_ADMIN_KEY) or add a config "
            "file and restart. Until then every publishable key returns 401."
        )
        return reg
    for path in files:
        reg.register(load_customer_file(path))
    return reg
