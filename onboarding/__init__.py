"""Onboarding: turn messy inputs into a proposed, validated ProductConfig a customer
approves in minutes -- the brief's "hidden product." You paste your pricing/docs and list
your offers; the LLM only *structures* it, `config.validate()` *guarantees* it, the human
*approves* it.
"""

from .proposer import (
    Proposal,
    ProposalError,
    ProposalInput,
    propose_config,
)

__all__ = ["propose_config", "Proposal", "ProposalInput", "ProposalError"]
