"""Randomized arm assignment for the holdout experiment.

A cancel-flow session is assigned to `control` (interviewed and diagnosed, but shown NO
offer) or `treatment` (offer served) BEFORE the offer decision runs. Comparing the two
arms' downstream retention is what turns the engine's hand-set `save_prior * effectiveness`
guesses into measured causal lift (see engine.taxonomy.Experiment, learning.experiment).

Assignment is DETERMINISTIC -- a hash of (experiment_id, customer_id, user_id), not an RNG.
Two reasons this matters:
  1. Stability: a retried /sessions call, or the same user returning, lands in the same arm.
     A user can't be held out today and treated tomorrow, which would contaminate the read.
  2. Testability & auditability: the arm is a pure function of its inputs, reproducible
     offline from the logs -- no hidden random state to reconcile.

The hash is mapped uniformly to [0, 1); the bottom `holdout_fraction` of that range is the
control arm. Because the hash is uniform, the control share converges to `holdout_fraction`.
Changing `experiment_id` re-draws every assignment (a clean new experiment), rather than
silently mixing two designs' users into one dataset.
"""

import hashlib

CONTROL = "control"
TREATMENT = "treatment"


def _bucket(experiment_id: str, customer_id: str, user_id: str) -> float:
    """Uniform hash in [0, 1) over the assignment key. SHA-256 (not Python's salted hash())
    so the value is stable across processes and reproducible from the logs later."""
    key = f"{experiment_id}\x1f{customer_id}\x1f{user_id}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    # First 8 bytes as an unsigned int, scaled to [0, 1). 2**64 is the full range.
    n = int.from_bytes(digest[:8], "big")
    return n / 2**64


def assign(
    experiment_id: str,
    customer_id: str,
    user_id: str,
    holdout_fraction: float,
) -> str:
    """Return CONTROL or TREATMENT for this session.

    `holdout_fraction <= 0` short-circuits to TREATMENT (experiment off -- the safe default,
    so a customer who hasn't opted in never has a save withheld). `>= 1` holds everyone out.
    """
    if holdout_fraction <= 0.0:
        return TREATMENT
    if holdout_fraction >= 1.0:
        return CONTROL
    return CONTROL if _bucket(experiment_id, customer_id, user_id) < holdout_fraction else TREATMENT
