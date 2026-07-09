"""The FastAPI app. Thin HTTP layer over api.service.

Endpoints (§4):
  GET  /health
  POST /sessions                 -> { session_id, message }
  POST /sessions/{id}/turn       -> { message, done } | { done: true, outcome }
  POST /sessions/{id}/resolution -> { status: "recorded", accepted }

Auth is a Bearer publishable key (`pk_...`) resolved against the customer registry. A
publishable key is public by design, so it is NOT the security boundary; the real controls
are (1) signed identity tokens -- the browser can't price its own save (see api.identity),
(2) a per-key + per-IP rate limit on the model-cost endpoints, and (3) a per-customer origin
allowlist. CORS stays permissive at the browser layer (a public API), which is why those
three, not CORS, carry the weight.
"""

import os
import time
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .ratelimit import RateLimiter, RateLimitExceeded
from .registry import Customer, CustomerRegistry, default_registry
from .schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    ResolutionRequest,
    TurnRequest,
    TurnResponse,
)
from .service import (
    IdentityRejected,
    SessionNotFound,
    record_resolution,
    run_turn,
    start_session,
)
from .store import SessionStore
from .transcripts import TranscriptLogger

# How long a single model call may run before we give up and surface a clean error instead of
# hanging the user's modal. Tunable via env for slower models / networks.
MODEL_TIMEOUT_SECONDS = float(os.getenv("OFFBOARD_MODEL_TIMEOUT", "30"))

# Fixed-window rate limits (hits, seconds). Sized so a real cancel flow never trips them but a
# scripted key/IP draining the model budget does.
SESSION_LIMIT_PER_KEY = (30, 60)
SESSION_LIMIT_PER_IP = (20, 60)
TURN_LIMIT_PER_KEY = (120, 60)
TURN_LIMIT_PER_IP = (120, 60)


def _lazy_anthropic_factory() -> Callable[[], object]:
    """Create the anthropic client on first use, not at import time -- so the app can
    be constructed (and health-checked) without ANTHROPIC_API_KEY present. A request timeout
    and a single retry cap tail latency so one slow call can't hang the modal indefinitely."""
    cached: dict[str, object] = {}

    def factory() -> object:
        if "client" not in cached:
            import anthropic

            cached["client"] = anthropic.Anthropic(
                timeout=MODEL_TIMEOUT_SECONDS, max_retries=1
            )
        return cached["client"]

    return factory


def create_app(
    registry: Optional[CustomerRegistry] = None,
    store: Optional[SessionStore] = None,
    logger: Optional[TranscriptLogger] = None,
    client_factory: Optional[Callable[[], object]] = None,
    allow_origins: Optional[list[str]] = None,
    limiter: Optional[RateLimiter] = None,
    now: Callable[[], float] = time.time,
) -> FastAPI:
    registry = registry or default_registry()
    store = store or SessionStore(now=now)
    logger = logger or TranscriptLogger()
    client_factory = client_factory or _lazy_anthropic_factory()
    limiter = limiter or RateLimiter(now=now)

    app = FastAPI(title="Offboard Engine API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _rate_limit(request: Request, customer: Customer, per_key, per_ip, tag: str) -> None:
        try:
            limiter.check(f"{tag}:key:{customer.id}", *per_key)
            limiter.check(f"{tag}:ip:{_client_ip(request)}", *per_ip)
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(exc.retry_after)},
            )

    def authenticate(
        authorization: str = Header(default=""),
        origin: str = Header(default=""),
    ) -> Customer:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="missing bearer token")
        customer = registry.get_by_key(authorization[len(prefix):].strip())
        if customer is None:
            raise HTTPException(status_code=401, detail="invalid public key")
        # Origin allowlist (defense in depth). A browser can't forge its Origin header, so an
        # unlisted site driving a lifted key from a page is blocked here.
        if customer.allowed_origins and origin and origin not in customer.allowed_origins:
            raise HTTPException(status_code=403, detail="origin not allowed")
        return customer

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", response_model=CreateSessionResponse)
    def create_session(
        req: CreateSessionRequest,
        request: Request,
        customer: Customer = Depends(authenticate),
    ) -> CreateSessionResponse:
        _rate_limit(request, customer, SESSION_LIMIT_PER_KEY, SESSION_LIMIT_PER_IP, "session")
        try:
            return start_session(store, client_factory(), customer, req, now=int(now()))
        except IdentityRejected as exc:
            raise HTTPException(status_code=401, detail=str(exc))

    @app.post("/sessions/{session_id}/turn", response_model=TurnResponse)
    def turn(
        session_id: str,
        req: TurnRequest,
        request: Request,
        customer: Customer = Depends(authenticate),
    ) -> TurnResponse:
        _rate_limit(request, customer, TURN_LIMIT_PER_KEY, TURN_LIMIT_PER_IP, "turn")
        try:
            return run_turn(store, customer, session_id, req, logger)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")

    @app.post("/sessions/{session_id}/resolution")
    def resolution(
        session_id: str,
        req: ResolutionRequest,
        request: Request,
        customer: Customer = Depends(authenticate),
    ) -> dict:
        _rate_limit(request, customer, TURN_LIMIT_PER_KEY, TURN_LIMIT_PER_IP, "resolution")
        try:
            return record_resolution(store, customer, session_id, req.accepted, logger)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail="session not found")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()
