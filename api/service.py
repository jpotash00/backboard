"""Orchestration: turn the session API calls into interviewer + policy runs.

Framework-agnostic on purpose (no FastAPI imports) so it's unit-testable and the HTTP
layer in app.py stays thin. The rule from the brief holds here: the interviewer
diagnoses, `policy.decide` authorizes -- the model never picks the intervention.
"""

import json
from dataclasses import asdict, replace
from uuid import uuid4

from engine import Interviewer, UserContext, decide

from .config_store import load_customer_file
from .experiment import CONTROL, assign
from .identity import IdentityError, verify_identity
from .registry import Customer
from .schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    InterventionModel,
    OutcomeModel,
    TurnRequest,
    TurnResponse,
)
from .store import SessionState, SessionStore
from .transcripts import TranscriptLogger


class SessionNotFound(Exception):
    """No live session with that id for this customer."""


class IdentityRejected(Exception):
    """The customer requires a signed identity token and none valid was supplied."""


class ConfigConflict(Exception):
    """A customer with that id or public key already exists (create-only endpoint)."""


def create_customer_from_spec(spec: dict, registry, config_dir: str) -> dict:
    """Provision a new customer at runtime: validate the spec, mint a signing secret, persist
    the file (so it survives restarts), and hot-register it into the LIVE registry -- the
    customer is usable on the next request, no restart. Reuses the exact same builder as the
    `onboarding.provision` CLI, so the endpoint and the file-drop path can't drift.

    Returns the full record (including the once-shown signing_secret). Raises ConfigConflict on
    a duplicate id/key, and onboarding.provision.ProvisionError on an invalid spec.

    NOTE: hot-register mutates THIS process's registry. In a multi-instance deployment the
    written file makes the customer live everywhere on each instance's next restart; immediate
    cross-instance liveness needs a shared registry (the DB-backed seam) -- see GUIDE §7."""
    # lazy: keeps the onboarding package off the hot session path's import graph
    from onboarding.provision import ProvisionError, build_record, write_customer_file

    if not config_dir:
        raise ProvisionError("OFFBOARD_CONFIG_DIR is not set; cannot persist a new customer")
    record = build_record(spec)  # validates config + mints signing_secret (or raises)
    if registry.get_by_key(record["public_key"]) is not None:
        raise ConfigConflict(f"public_key already registered: {record['public_key']}")
    if registry.get_by_id(record["customer_id"]) is not None:
        raise ConfigConflict(f"customer_id already exists: {record['customer_id']}")
    path = write_customer_file(record, config_dir)  # no-clobber on disk too
    registry.register(load_customer_file(path))     # hot-register: live, no restart
    return record


def start_session(
    store: SessionStore,
    client,
    customer: Customer,
    req: CreateSessionRequest,
    now: int,
) -> CreateSessionResponse:
    user = req_to_user(req, customer, now)
    interviewer = Interviewer(customer.config, user, client=client)
    first_question = interviewer.open()

    # Assign the experiment arm up front, before any offer is decided. Control users are still
    # fully interviewed (we keep their diagnosis) but will have the offer withheld -- that's
    # the baseline retention the causal read compares against. See api.experiment.
    exp = customer.config.experiment
    arm = assign(exp.experiment_id, customer.id, user.user_id, exp.holdout_fraction)

    session_id = uuid4().hex
    state = SessionState(
        session_id=session_id,
        customer_id=customer.id,
        interviewer=interviewer,
        config=customer.config,
        user=user,
        transcript=[{"speaker": "interviewer", "text": first_question}],
        arm=arm,
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
            intervention=_resolve_intervention(state, state.outcome.intervention_id),
        )

    state.transcript.append({"speaker": "churner", "text": req.user_message})
    question, outcome = state.interviewer.turn(req.user_message)

    if question is not None:
        state.transcript.append({"speaker": "interviewer", "text": question})
        store.save(state)
        return TurnResponse(message=question, done=False)

    # Diagnosed. Policy authorizes the intervention from the customer's own menu. This runs
    # for BOTH arms -- the decision is what we record as `intended`, the counterfactual that
    # makes control comparable to treatment. The offer is only WITHHELD, never un-decided.
    final = decide(outcome, state.config, state.user)
    state.intended_intervention_id = final.intervention_id

    served = _serve(final, state.arm)
    closing = _closing_message(state.interviewer)

    state.outcome = served
    state.closing_message = closing
    state.done = True
    if closing:
        state.transcript.append({"speaker": "interviewer", "text": closing})
    store.save(state)
    logger.log(state)

    return TurnResponse(
        message=closing,
        done=True,
        outcome=OutcomeModel(**asdict(served)),
        intervention=_resolve_intervention(state, served.intervention_id),
    )


def _serve(final, arm: str):
    """What the user actually sees. Treatment gets the decided offer; control gets NO offer
    (the diagnosis is kept, the intervention is stripped) so it falls to the host's normal
    cancel flow -- the clean baseline. The full decision_trace/economics stay on the outcome
    for audit; only the servable `intervention_id` is cleared, which is what gates rendering."""
    if arm != CONTROL:
        return final
    would_have = final.intervention_id or "nothing"
    return replace(
        final,
        intervention_id=None,
        savable=False,
        mode="defer",
        rationale=(f"holdout control -- offer withheld to measure baseline retention "
                   f"(would have served: {would_have})"),
    )


def _resolve_intervention(state: SessionState, intervention_id):
    """Spell out the authorized intervention so the host can render the offer directly.
    None when policy authorized nothing (below floor, let-go, or no match)."""
    if not intervention_id:
        return None
    iv = next((i for i in state.config.interventions if i.id == intervention_id), None)
    if iv is None:
        return None
    return InterventionModel(id=iv.id, type=iv.type, description=iv.description)


def record_resolution(
    store: SessionStore,
    customer: Customer,
    session_id: str,
    accepted: bool,
    logger: TranscriptLogger,
) -> dict:
    """Log what the user did with the offer. Idempotent -- a repeat call (double-tap,
    retry) records once. This is the realized-save ground truth for recalibration."""
    state = store.get(session_id)
    if state is None or state.customer_id != customer.id:
        raise SessionNotFound(session_id)
    if state.resolution is None:
        state.resolution = {"accepted": bool(accepted)}
        logger.log_resolution(state, accepted)
        store.save(state)
    return {"status": "recorded", "accepted": state.resolution["accepted"]}


def record_outcome(
    customer: Customer,
    user_id: str,
    active: bool,
    observed_at: str,
    logger: TranscriptLogger,
) -> dict:
    """Ingest a downstream retention observation for a user. Stateless -- it does not touch
    the (ephemeral) session store; it appends to the durable outcome log, joined to the arm
    offline. Reporting for a user we never offered to is harmless (it just won't join)."""
    logger.log_outcome(customer.id, user_id, active, observed_at)
    return {"status": "recorded", "user_id": user_id, "active": bool(active)}


def req_to_user(req: CreateSessionRequest, customer: Customer, now: int) -> UserContext:
    """Build the UserContext policy will price the save on.

    If the customer has a signing secret, the ONLY trusted source of economics is the signed
    identity token -- the raw request body is treated as hostile and its money fields are
    ignored. Without a secret (dev / demo) we trust the body, which is convenient and clearly
    insecure; production customers get a secret at onboarding."""
    if customer.signing_secret:
        if not req.identity_token:
            raise IdentityRejected("this key requires a signed identity_token")
        try:
            claims = verify_identity(customer.signing_secret, req.identity_token, now)
        except IdentityError as exc:
            raise IdentityRejected(str(exc)) from exc
        return _user_from_claims(claims)
    return _user_from_req(req)


def _user_from_req(req: CreateSessionRequest) -> UserContext:
    return UserContext(
        user_id=req.user_id,
        plan=req.plan,
        mrr=req.mrr,
        tenure_days=req.tenure_days,
        logins_last_30d=req.logins_last_30d,
        activated=req.activated,
        usage_summary=req.usage_summary,
        signals=req.signals,
    )


def _user_from_claims(claims: dict) -> UserContext:
    """The token is the source of truth. Coerce defensively -- a customer's signer might omit
    a field -- but never fall back to the request body for anything that authorizes a spend."""
    try:
        user_id = str(claims["user_id"])
    except KeyError as exc:
        raise IdentityRejected("identity token missing user_id") from exc
    signals = claims.get("signals") or {}
    if not isinstance(signals, dict):
        signals = {}
    return UserContext(
        user_id=user_id,
        plan=str(claims.get("plan", "unknown")),
        mrr=float(claims.get("mrr", 0.0) or 0.0),
        tenure_days=int(claims.get("tenure_days", 0) or 0),
        logins_last_30d=int(claims.get("logins_last_30d", 0) or 0),
        activated=bool(claims.get("activated", False)),
        usage_summary=str(claims.get("usage_summary", "") or ""),
        signals=signals,
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
