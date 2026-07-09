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
    raises at load with a clear message instead of failing mid-interview."""
    data = json.loads(Path(path).read_text())
    return Customer(
        id=data["customer_id"],
        public_key=data["public_key"],
        config=config_from_dict(data["config"]),
    )


def registry_from_dir(config_dir: Union[str, Path]) -> CustomerRegistry:
    """Build a registry from every *.json in a directory. Fails fast on a bad file."""
    reg = CustomerRegistry()
    files = sorted(Path(config_dir).glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no *.json customer configs found in {config_dir}")
    for path in files:
        reg.register(load_customer_file(path))
    return reg
