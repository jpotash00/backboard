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

import hmac
import os
import time
from typing import Callable, Optional

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .ratelimit import RateLimiter, RateLimitExceeded
from .registry import Customer, CustomerRegistry, default_registry
from .schemas import (
    ConfigSpecRequest,
    ConfigUpdateRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    OutcomeReport,
    ResolutionRequest,
    TurnRequest,
    TurnResponse,
)
from .service import (
    ConfigConflict,
    CustomerNotFound,
    IdentityRejected,
    SessionNotFound,
    create_customer_from_spec,
    delete_customer,
    list_customers,
    record_outcome,
    record_resolution,
    run_turn,
    start_session,
    update_customer,
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


def _resolve_client_ip(peer: str, forwarded: str, trusted_proxy_hops: int) -> str:
    """The real client IP for per-IP rate limiting. Directly on the socket, that's the peer
    address. Behind proxies the peer is the last-hop proxy instead, so when `trusted_proxy_hops
    > 0` we read X-Forwarded-For and take the entry that many hops in from the RIGHT -- the
    address the outermost trusted proxy actually saw. Indexing from the right is what makes it
    unspoofable: a client can prepend extra XFF entries, but they land left of the trusted
    segment and are ignored. Falls back to the socket peer when the header is missing or shorter
    than the configured hop count, so a misconfiguration can't silently disable the limit by
    collapsing every request onto one empty key."""
    if trusted_proxy_hops <= 0:
        return peer
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if len(parts) >= trusted_proxy_hops:
        return parts[-trusted_proxy_hops]
    return peer


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
    admin_key: Optional[str] = None,
    config_dir: Optional[str] = None,
    trusted_proxy_hops: Optional[int] = None,
    signup_key: Optional[str] = None,
) -> FastAPI:
    registry = registry or default_registry()
    client_factory = client_factory or _lazy_anthropic_factory()
    # Admin surface (POST /configs). `admin_key` is a SECRET bearer token, distinct from any
    # publishable key -- unset means the endpoint is disabled (404). `config_dir` is where new
    # customer files are persisted (defaults to the same dir the registry loaded from).
    admin_key = admin_key if admin_key is not None else os.getenv("OFFBOARD_ADMIN_KEY")
    config_dir = config_dir if config_dir is not None else os.getenv("OFFBOARD_CONFIG_DIR")
    # Self-serve onboarding surface (POST/GET /onboard). A SEPARATE secret from the master
    # admin_key: it's create-only (a customer can provision ITSELF but can't list/delete/read
    # other tenants), and it's rotatable if a shared signup link leaks. Unset = self-serve is
    # disabled (404), same fail-safe stance as the admin surface.
    signup_key = signup_key if signup_key is not None else os.getenv("OFFBOARD_SIGNUP_KEY")
    # Backend selection: a shared Redis store (multiple instances / zero-downtime deploys) when
    # OFFBOARD_REDIS_URL is set, else the process-local store (single instance). Both the store
    # AND the rate limiter follow this switch -- a per-process limiter on a multi-instance deploy
    # would let the effective limit scale to N x the configured one, so the shared session
    # topology and the shared limiter go together. All backends share one create/get/save (store)
    # and one check (limiter) contract, so nothing downstream changes.
    redis_url = os.getenv("OFFBOARD_REDIS_URL")
    if store is None:
        if redis_url:
            from .store import redis_store_from_url
            store = redis_store_from_url(redis_url, registry, client_factory)
        else:
            store = SessionStore(now=now)
    # The data asset (transcripts / resolutions / outcomes) is append-only JSONL. On an
    # ephemeral container filesystem it must live on a mounted volume or it's wiped every
    # redeploy -- point OFFBOARD_RUNS_DIR at that volume. See docs/DEPLOY.md.
    logger = logger or TranscriptLogger(directory=os.getenv("OFFBOARD_RUNS_DIR", "runs"))
    if limiter is None:
        if redis_url:
            from .ratelimit import redis_ratelimiter_from_url
            limiter = redis_ratelimiter_from_url(redis_url, now=now)
        else:
            limiter = RateLimiter(now=now)

    # How many trusted proxy hops sit in front of the app, for reading the real client IP from
    # X-Forwarded-For. 0 (default) = trust nobody, use the socket peer. Behind an edge/ingress
    # (e.g. Fly, an ALB) the socket peer is the PROXY, not the user, which would collapse the
    # per-IP rate limit into a single global bucket -- set OFFBOARD_TRUSTED_PROXY_HOPS to the
    # number of proxies (1 for a single edge) so the per-IP limit actually bites. See _client_ip.
    if trusted_proxy_hops is None:
        trusted_proxy_hops = int(os.getenv("OFFBOARD_TRUSTED_PROXY_HOPS", "0"))

    # `root_path` tells FastAPI it's served under a path prefix by an upstream proxy (e.g. an
    # ingress mapping `/v1/* -> /*`), so generated docs/OpenAPI URLs stay correct. Empty by
    # default: the app serves at the root and the SDK's base URL carries no version prefix. If
    # you expose it under `/v1`, set OFFBOARD_ROOT_PATH=/v1 here and include `/v1` in the SDK URL.
    app = FastAPI(
        title="Offboard Engine API",
        version="0.1.0",
        root_path=os.getenv("OFFBOARD_ROOT_PATH", ""),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_methods=["POST", "GET", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    def _client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for", "")
        return _resolve_client_ip(peer, forwarded, trusted_proxy_hops)

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

    @app.post("/outcomes")
    def outcomes(
        req: OutcomeReport,
        request: Request,
        customer: Customer = Depends(authenticate),
    ) -> dict:
        # Downstream retention ground truth (see api.experiment). Rate-limited like the other
        # write endpoints; it's cheap (no model call) but still a public, authenticated write.
        _rate_limit(request, customer, TURN_LIMIT_PER_KEY, TURN_LIMIT_PER_IP, "outcome")
        return record_outcome(customer, req.user_id, req.active, req.observed_at, logger)

    def authenticate_admin(authorization: str = Header(default="")) -> None:
        """Admin auth for /configs. A SECRET bearer token (OFFBOARD_ADMIN_KEY), not a
        publishable key -- provisioning tenants can't be behind a public credential. When no
        admin key is configured the endpoint is disabled (404), so it can never sit open."""
        if not admin_key:
            raise HTTPException(status_code=404, detail="not found")
        prefix = "Bearer "
        presented = authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""
        # constant-time compare so a wrong key can't be discovered by timing.
        if not presented or not hmac.compare_digest(presented, admin_key):
            raise HTTPException(status_code=401, detail="invalid admin key")

    def _provision(req: ConfigSpecRequest) -> dict:
        # Shared provisioning body for BOTH the admin (/configs) and self-serve (/onboard)
        # surfaces, so they can't drift. Validate + mint secret + persist + hot-register; the
        # customer is usable on the next request with no restart. `signing_secret` is returned
        # ONCE -- the caller must store it (their backend signs identity tokens with it).
        from onboarding.provision import ProvisionError

        try:
            record = create_customer_from_spec(req.model_dump(), registry, config_dir)
        except ConfigConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProvisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "status": "registered",
            "customer_id": record["customer_id"],
            "public_key": record["public_key"],
            "signing_secret": record["signing_secret"],
            "offers": len(record["config"]["interventions"]),
        }

    @app.post("/configs", status_code=201)
    def create_config(req: ConfigSpecRequest, _: None = Depends(authenticate_admin)) -> dict:
        return _provision(req)

    @app.get("/configs")
    def list_configs(_: None = Depends(authenticate_admin)) -> dict:
        # Non-secret roster for the admin console. Same gate as create; never returns a secret.
        return {"customers": list_customers(registry)}

    @app.get("/admin", include_in_schema=False)
    def admin_console() -> FileResponse:
        # The operator's onboarding cockpit. Static form -- it holds no secret itself (the admin
        # token is entered at runtime and lives only in the browser), so serving the HTML is safe;
        # every action it takes still goes through the admin-gated endpoints above. Disabled (404)
        # whenever the admin surface is off, so it can't sit open advertising the API shape.
        if not admin_key:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(Path(__file__).parent / "admin.html")

    def authenticate_signup(authorization: str = Header(default="")) -> None:
        """Self-serve onboarding auth for /onboard. A shared SIGNUP secret (OFFBOARD_SIGNUP_KEY),
        distinct from the master admin key -- a customer provisions ITSELF with this, but it grants
        no read/list/delete over other tenants. Disabled (404) when unset, so it never sits open."""
        if not signup_key:
            raise HTTPException(status_code=404, detail="not found")
        prefix = "Bearer "
        presented = authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""
        if not presented or not hmac.compare_digest(presented, signup_key):
            raise HTTPException(status_code=401, detail="invalid signup code")

    @app.post("/onboard", status_code=201)
    def self_serve_onboard(req: ConfigSpecRequest, _: None = Depends(authenticate_signup)) -> dict:
        # Customer-driven provisioning: same create-only body as /configs, gated by the signup
        # code instead of the admin key. This is what lets a customer set themselves up from a
        # shared link with no operator in the loop.
        return _provision(req)

    @app.get("/onboard", include_in_schema=False)
    def onboard_page() -> FileResponse:
        # The customer-facing signup form. Reads its signup code from the URL (?code=...), so a
        # shared link is one click to a working form; holds no secret itself. 404 when self-serve
        # is off, so an unconfigured deployment doesn't advertise a signup surface.
        if not signup_key:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(Path(__file__).parent / "onboard.html")

    def authorize_manage(customer_id: str, authorization: str) -> None:
        """Auth for editing/deleting a specific tenant. Accepts EITHER the master admin key (you,
        as superuser, can manage any tenant for support) OR that tenant's own signing_secret (the
        customer, from their backend, can manage ONLY their own). Isolation is automatic: you can
        only touch a tenant whose secret you hold, and each customer holds only their own. A wrong
        or missing credential is a flat 401 whether or not the tenant exists (no existence leak)."""
        prefix = "Bearer "
        presented = authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""
        if not presented:
            raise HTTPException(status_code=401, detail="missing bearer token")
        if admin_key and hmac.compare_digest(presented, admin_key):
            return  # superuser
        customer = registry.get_by_id(customer_id)
        if customer and customer.signing_secret and hmac.compare_digest(presented, customer.signing_secret):
            return  # tenant owner, via its own signing_secret
        raise HTTPException(status_code=401, detail="not authorized to manage this customer")

    @app.patch("/configs/{customer_id}")
    def update_config(
        customer_id: str, req: ConfigUpdateRequest, authorization: str = Header(default="")
    ) -> dict:
        # Self-serve or admin edit of a tenant's mutable config. Preserves customer_id/public_key/
        # signing_secret so a live SDK and in-flight identity tokens keep working across the edit.
        from onboarding.provision import ProvisionError

        authorize_manage(customer_id, authorization)
        try:
            return update_customer(customer_id, req.model_dump(), registry, config_dir)
        except CustomerNotFound:
            raise HTTPException(status_code=404, detail="customer not found")
        except ProvisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.delete("/configs/{customer_id}")
    def delete_config(customer_id: str, authorization: str = Header(default="")) -> dict:
        # De-provision a tenant: its key stops resolving on the next request and the file is removed.
        authorize_manage(customer_id, authorization)
        try:
            return delete_customer(customer_id, registry, config_dir)
        except CustomerNotFound:
            raise HTTPException(status_code=404, detail="customer not found")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()
