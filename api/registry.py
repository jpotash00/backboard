"""Customer registry: maps a publishable key to a customer's ProductConfig.

In production this is a database row created at onboarding (the brief's "hidden
product": scrape pricing page + docs, propose config, approve in five minutes). For
Milestone 2 it's an in-memory map seeded with one demo customer.
"""

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


class CustomerRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, Customer] = {}

    def register(self, customer: Customer) -> None:
        self._by_key[customer.public_key] = customer

    def get_by_key(self, public_key: str) -> Optional[Customer]:
        return self._by_key.get(public_key)


def default_registry() -> CustomerRegistry:
    reg = CustomerRegistry()
    reg.register(Customer(id="acme", public_key=DEMO_PUBLIC_KEY, config=ACME))
    return reg
