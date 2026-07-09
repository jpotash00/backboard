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
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def log_resolution(self, state: SessionState, accepted: bool) -> None:
        """One self-contained record per offer decision -- everything needed to fit save
        rates without joining back to the session log. `offered` gates the denominator:
        realized save rate = accepted / offered, sliced by reason and intervention type."""
        self.resolutions_path.parent.mkdir(parents=True, exist_ok=True)
        outcome = state.outcome
        iv_id = outcome.intervention_id if outcome else None
        iv_type = next(
            (i.type for i in state.config.interventions if i.id == iv_id), None
        )
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id,
            "customer_id": state.customer_id,
            "user_id": state.user.user_id,
            "reason": outcome.reason if outcome else None,
            "mode": outcome.mode if outcome else None,
            "intervention_id": iv_id,
            "intervention_type": iv_type,
            "offered": iv_id is not None,
            "accepted": bool(accepted),
        }
        with self.resolutions_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
