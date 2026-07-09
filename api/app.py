"""The FastAPI app. Thin HTTP layer over api.service.

Endpoints (§4):
  GET  /health
  POST /sessions                 -> { session_id, message }
  POST /sessions/{id}/turn       -> { message, done } | { done: true, outcome }

Auth is a Bearer publishable key (`pk_...`) resolved against the customer registry.
The SDK runs in the browser, so CORS is open by default (tighten per-customer later).
"""

import os
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .registry import Customer, CustomerRegistry, default_registry
from .schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    TurnRequest,
    TurnResponse,
)
from .service import SessionNotFound, run_turn, start_session
from .store import SessionStore
from .transcripts import TranscriptLogger


def _lazy_anthropic_factory() -> Callable[[], object]:
    """Create the anthropic client on first use, not at import time -- so the app can
    be constructed (and health-checked) without ANTHROPIC_API_KEY present."""
    cached: dict[str, object] = {}

    def factory() -> object:
        if "client" not in cached:
            import anthropic

            cached["client"] = anthropic.Anthropic()
        return cached["client"]

    return factory


def create_app(
    registry: Optional[CustomerRegistry] = None,
    store: Optional[SessionStore] = None,
    logger: Optional[TranscriptLogger] = None,
    client_factory: Optional[Callable[[], object]] = None,
    allow_origins: Optional[list[str]] = None,
) -> FastAPI:
    registry = registry or default_registry()
    store = store or SessionStore()
    logger = logger or TranscriptLogger()
    client_factory = client_factory or _lazy_anthropic_factory()

    app = FastAPI(title="Offboard Engine API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    def authenticate(authorization: str = Header(default="")) -> Customer:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="missing bearer token")
        customer = registry.get_by_key(authorization[len(prefix):].strip())
        if customer is None:
            raise HTTPException(status_code=401, detail="invalid public key")
        return customer

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", response_model=CreateSessionResponse)
    def create_session(
        req: CreateSessionRequest,
        customer: Customer = Depends(authenticate),
    ) -> CreateSessionResponse:
        return start_session(store, client_factory(), customer, req)

    @app.post("/sessions/{session_id}/turn", response_model=TurnResponse)
    def turn(
        session_id: str,
        req: TurnRequest,
        customer: Customer = Depends(authenticate),
    ) -> TurnResponse:
        try:
            return run_turn(store, customer, session_id, req, logger)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()
