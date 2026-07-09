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
