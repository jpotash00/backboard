"""Input adapters: ways to gather the `product_text` the proposer structures. Scraping is
just ONE adapter, not the spine -- it's the nice-case autofill. When a site isn't
scrapeable (pricing behind "contact sales", docs behind a login, a JS-rendered SPA), the
paste adapter always works, because the customer has their own copy.

The offer menu is never gathered here -- it's a business decision the customer states
directly (see ProposalInput.offers).
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class InputAdapter(Protocol):
    def gather(self) -> str:
        """Return product text (pricing / docs) for the proposer to structure."""
        ...


class PasteAdapter:
    """The always-available path: the customer pastes their pricing/docs text."""
    def __init__(self, text: str):
        self._text = text

    def gather(self) -> str:
        return self._text


class UrlScrapeAdapter:
    """Best-case autofill: fetch + strip a public pricing/docs page. Stub -- wire a fetcher
    (and a JS-render fallback) here. Must fail soft to the paste path when unavailable."""
    def __init__(self, url: str):
        self.url = url

    def gather(self) -> str:
        raise NotImplementedError(
            "UrlScrapeAdapter is a stub. Implement a fetcher, or use PasteAdapter. "
            "Scraping is optional autofill; the paste path always works."
        )


class StripeAdapter:
    """Derive plans + real price points from the customer's billing (more reliable than
    scraping a marketing page). Stub -- wire the Stripe API here."""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def gather(self) -> str:
        raise NotImplementedError(
            "StripeAdapter is a stub. Query Stripe Prices/Products and format them here."
        )
