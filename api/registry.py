"""Customer registry: maps a publishable key to a customer's ProductConfig.

In production this is a database row created at onboarding (the brief's "hidden
product": scrape pricing page + docs, propose config, approve in five minutes). For
Milestone 2 it's an in-memory map seeded with one demo customer.
"""

import os
from dataclasses import dataclass
from typing import Optional

from engine import ProductConfig
# The demo customer reuses the eval's Acme fixture -- it's plain config data (no test
# framework), and keeping one definition avoids drift. In production this comes from a DB.
from eval.configs import ACME

DEMO_PUBLIC_KEY = "pk_demo_acme"


@dataclass
class Customer:
    id: str
    public_key: str
    config: ProductConfig
    # HMAC secret the customer's backend signs identity tokens with. When set, the browser's
    # raw economic fields are ignored and only signed claims authorize a paid offer (see
    # api.identity). None = dev/unsecured mode: the request body is trusted as-is.
    signing_secret: Optional[str] = None
    # Browser origins allowed to call the API with this key. Empty = allow any (dev). When
    # set, a cross-origin request from an unlisted site is rejected 403 -- defense in depth
    # against a lifted key being driven from an attacker's page.
    allowed_origins: tuple[str, ...] = ()


class CustomerRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, Customer] = {}

    def register(self, customer: Customer) -> None:
        self._by_key[customer.public_key] = customer

    def get_by_key(self, public_key: str) -> Optional[Customer]:
        return self._by_key.get(public_key)

    def get_by_id(self, customer_id: str) -> Optional[Customer]:
        """Resolve a customer by internal id. The Redis session store needs this: a snapshot
        carries only `customer_id`, and rehydrating it must re-attach that customer's config."""
        return next((c for c in self._by_key.values() if c.id == customer_id), None)

    def customers(self) -> list[Customer]:
        """Every registered customer. For the admin read surface (GET /configs) -- callers must
        summarize, never leak `signing_secret`."""
        return list(self._by_key.values())


def default_registry() -> CustomerRegistry:
    """If OFFBOARD_CONFIG_DIR is set, load customers from stored JSON configs (the
    production path). Otherwise seed the single in-code Acme demo customer."""
    config_dir = os.getenv("OFFBOARD_CONFIG_DIR")
    if config_dir:
        from .config_store import registry_from_dir  # lazy: avoids an import cycle
        return registry_from_dir(config_dir)

    reg = CustomerRegistry()
    reg.register(Customer(id="acme", public_key=DEMO_PUBLIC_KEY, config=ACME))
    return reg
