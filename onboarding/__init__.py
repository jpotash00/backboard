"""Onboarding: turn messy inputs into a proposed, validated ProductConfig a customer
approves in minutes -- the brief's "hidden product." Input-agnostic (paste / form / an
adapter); the LLM only *structures*, `config.validate()` *guarantees*, the human *approves*.
"""

from .proposer import (
    Proposal,
    ProposalError,
    ProposalInput,
    propose_config,
)

__all__ = ["propose_config", "Proposal", "ProposalInput", "ProposalError"]
