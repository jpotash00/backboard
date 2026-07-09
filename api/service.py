"""Orchestration: turn the session API calls into interviewer + policy runs.

Framework-agnostic on purpose (no FastAPI imports) so it's unit-testable and the HTTP
layer in app.py stays thin. The rule from the brief holds here: the interviewer
diagnoses, `policy.decide` authorizes -- the model never picks the intervention.
"""

import json
from dataclasses import asdict
from uuid import uuid4

from engine import Interviewer, decide

from .registry import Customer
from .schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    OutcomeModel,
    TurnRequest,
    TurnResponse,
)
from .store import SessionState, SessionStore
from .transcripts import TranscriptLogger


class SessionNotFound(Exception):
    """No live session with that id for this customer."""


def start_session(
    store: SessionStore,
    client,
    customer: Customer,
    req: CreateSessionRequest,
) -> CreateSessionResponse:
    user = req_to_user(req)
    interviewer = Interviewer(customer.config, user, client=client)
    first_question = interviewer.open()

    session_id = uuid4().hex
    state = SessionState(
        session_id=session_id,
        customer_id=customer.id,
        interviewer=interviewer,
        config=customer.config,
        user=user,
        transcript=[{"speaker": "interviewer", "text": first_question}],
    )
    store.create(state)
    return CreateSessionResponse(session_id=session_id, message=first_question)


def run_turn(
    store: SessionStore,
    customer: Customer,
    session_id: str,
    req: TurnRequest,
    logger: TranscriptLogger,
) -> TurnResponse:
    state = store.get(session_id)
    if state is None or state.customer_id != customer.id:
        raise SessionNotFound(session_id)

    # Idempotent: a repeated call on a resolved session replays the outcome.
    if state.done and state.outcome is not None:
        return TurnResponse(
            message=state.closing_message,
            done=True,
            outcome=OutcomeModel(**asdict(state.outcome)),
        )

    state.transcript.append({"speaker": "churner", "text": req.user_message})
    question, outcome = state.interviewer.turn(req.user_message)

    if question is not None:
        state.transcript.append({"speaker": "interviewer", "text": question})
        store.save(state)
        return TurnResponse(message=question, done=False)

    # Diagnosed. Policy authorizes the intervention from the customer's own menu.
    final = decide(outcome, state.config, state.user)
    closing = _closing_message(state.interviewer)

    state.outcome = final
    state.closing_message = closing
    state.done = True
    if closing:
        state.transcript.append({"speaker": "interviewer", "text": closing})
    store.save(state)
    logger.log(state)

    return TurnResponse(
        message=closing,
        done=True,
        outcome=OutcomeModel(**asdict(final)),
    )


def req_to_user(req: CreateSessionRequest):
    from engine import UserContext

    return UserContext(
        user_id=req.user_id,
        plan=req.plan,
        mrr=req.mrr,
        tenure_days=req.tenure_days,
        logins_last_30d=req.logins_last_30d,
        activated=req.activated,
        usage_summary=req.usage_summary,
    )


def _closing_message(interviewer: Interviewer):
    """The interviewer's warm closing sentence lives in the last assistant message's
    JSON `message` field; the Outcome doesn't carry it, so read it back here."""
    if not interviewer.messages:
        return None
    last = interviewer.messages[-1]
    if last.get("role") != "assistant":
        return None
    try:
        return json.loads(last["content"]).get("message")
    except (json.JSONDecodeError, TypeError, KeyError):
        return None
