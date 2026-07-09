"""Structured logging of every completed transcript + outcome. This is the data asset
that compounds -- aggregate churn intelligence (the later dashboard) is built on it, and
it's what lets you replay real sessions as new eval personas.

Two privacy controls live here so the asset is safe to keep:

  - Pseudonymized identity. The raw `user_id` never lands in the logs; it's replaced by
    HMAC(pepper, user_id) using OFFBOARD_LOG_PEPPER. The hash is deterministic, so the
    resolution -> outcome -> session joins the flywheel needs still work, but the customer's
    real identifier isn't sitting in plaintext. The pepper is write-once: rotating it would
    break joins to historical rows (the raw ids are gone), so it must stay stable -- set it
    once at first deploy. Unset (dev) = raw id passes through.

  - Date-partitioned files (api.runs_io), so retention and per-user erasure are possible at
    all -- see api.retention.
"""

import hashlib
import hmac
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .runs_io import partition_path
from .store import SessionState


def _pseudonymize(user_id: str, pepper: str) -> str:
    """HMAC the user id under the pepper. Deterministic (joins survive), one-way (the log can't
    be reversed to the real id without the pepper). No pepper -> return the raw id, so dev/tests
    are unchanged and only a configured deployment pseudonymizes."""
    if not pepper:
        return user_id
    digest = hmac.new(pepper.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return "u_" + digest[:32]


class TranscriptLogger:
    def __init__(self, directory: str = "runs") -> None:
        self.directory = Path(directory)
        # Pepper read once at construction. Empty string = pseudonymization off (dev).
        self._pepper = os.getenv("OFFBOARD_LOG_PEPPER", "")

    def _pid(self, user_id: str) -> str:
        return _pseudonymize(user_id, self._pepper)

    def _append(self, stem: str, record: dict) -> None:
        path = partition_path(self.directory, stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def log(self, state: SessionState) -> None:
        pid = self._pid(state.user.user_id)
        user_context = asdict(state.user)
        user_context["user_id"] = pid  # scrub the copy nested in the context too
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id,
            "customer_id": state.customer_id,
            "user_id": pid,
            "user_context": user_context,
            "transcript": state.transcript,
            "outcome": asdict(state.outcome) if state.outcome else None,
            "arm": state.arm,
            "intended_intervention_id": state.intended_intervention_id,
        }
        self._append("sessions", record)

    def log_resolution(self, state: SessionState, accepted: bool) -> None:
        """One self-contained record per session outcome -- everything the readouts need
        without joining back to the session log.

        Two denominators live here, and the distinction is the whole point of the holdout:
          - `offered` gates the OBSERVATIONAL accept rate = accepted / offered. Only the
            treatment arm with a served offer counts (control's `intervention_id` is None).
          - `arm` + `intended_intervention_type` gate the CAUSAL read: control rows carry the
            counterfactual offer they would have gotten, so retention can be compared per cell.
        `mrr` is stamped here so net-value (lift x LTV - cost) needs no session-log join.
        `user_id` is pseudonymized (see module docstring) but stays the join key to outcomes."""
        outcome = state.outcome
        served_id = outcome.intervention_id if outcome else None
        served_type = self._type_of(state, served_id)
        intended_id = state.intended_intervention_id
        intended_type = self._type_of(state, intended_id)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id,
            "customer_id": state.customer_id,
            "user_id": self._pid(state.user.user_id),
            "mrr": state.user.mrr,
            "arm": state.arm,
            "reason": outcome.reason if outcome else None,
            "mode": outcome.mode if outcome else None,
            "intervention_id": served_id,
            "intervention_type": served_type,
            # The offer policy WOULD serve regardless of arm -- the cell key for the causal read.
            "intended_intervention_id": intended_id,
            "intended_intervention_type": intended_type,
            "offered": served_id is not None,
            "accepted": bool(accepted),
            # LLM-extracted conversational signals, banked per row so the causal readout can
            # later segment lift by them (does counterfactual.response predict a real save?).
            # Log-only: nothing here has touched the offer decision. {} when none were emitted.
            "observations": outcome.observations if outcome else {},
        }
        self._append("resolutions", record)

    @staticmethod
    def _type_of(state: SessionState, intervention_id) -> "str | None":
        if not intervention_id:
            return None
        return next(
            (i.type for i in state.config.interventions if i.id == intervention_id), None
        )

    def log_outcome(
        self, customer_id: str, user_id: str, active: bool, observed_at: str
    ) -> None:
        """The downstream ground truth: was this user still subscribed when the customer's
        billing system last checked? Reported via POST /outcomes, keyed by (customer, user)
        and joined to the resolution log by the readout. Append-only: several checks over
        time are fine -- the readout takes the observation at/after the config's horizon.

        The raw user_id arrives from the customer's backend and is pseudonymized here with the
        SAME pepper as the resolution log, so the (customer, user) join still lines up."""
        self._append(
            "outcomes",
            {
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "customer_id": customer_id,
                "user_id": self._pid(user_id),
                "active": bool(active),
                "observed_at": observed_at,
            },
        )
