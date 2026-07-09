"""Structured logging of every completed transcript + outcome. This is the data asset
that compounds -- aggregate churn intelligence (the later dashboard) is built on it, and
it's what lets you replay real sessions as new eval personas."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .store import SessionState


class TranscriptLogger:
    def __init__(self, directory: str = "runs") -> None:
        self.path = Path(directory) / "sessions.jsonl"
        self.resolutions_path = Path(directory) / "resolutions.jsonl"
        self.outcomes_path = Path(directory) / "outcomes.jsonl"

    def log(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id,
            "customer_id": state.customer_id,
            "user_id": state.user.user_id,
            "user_context": asdict(state.user),
            "transcript": state.transcript,
            "outcome": asdict(state.outcome) if state.outcome else None,
            "arm": state.arm,
            "intended_intervention_id": state.intended_intervention_id,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def log_resolution(self, state: SessionState, accepted: bool) -> None:
        """One self-contained record per session outcome -- everything the readouts need
        without joining back to the session log.

        Two denominators live here, and the distinction is the whole point of the holdout:
          - `offered` gates the OBSERVATIONAL accept rate = accepted / offered. Only the
            treatment arm with a served offer counts (control's `intervention_id` is None).
          - `arm` + `intended_intervention_type` gate the CAUSAL read: control rows carry the
            counterfactual offer they would have gotten, so retention can be compared per cell.
        `mrr` is stamped here so net-value (lift x LTV - cost) needs no session-log join."""
        self.resolutions_path.parent.mkdir(parents=True, exist_ok=True)
        outcome = state.outcome
        served_id = outcome.intervention_id if outcome else None
        served_type = self._type_of(state, served_id)
        intended_id = state.intended_intervention_id
        intended_type = self._type_of(state, intended_id)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id,
            "customer_id": state.customer_id,
            "user_id": state.user.user_id,
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
        with self.resolutions_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

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
        time are fine -- the readout takes the observation at/after the config's horizon."""
        self.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "customer_id": customer_id,
            "user_id": user_id,
            "active": bool(active),
            "observed_at": observed_at,
        }
        with self.outcomes_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
