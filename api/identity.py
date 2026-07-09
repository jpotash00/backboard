"""Signed identity tokens: how a customer's backend vouches for a user's economics.

The cancel flow runs in the browser, so everything the browser sends is attacker-controlled.
The fields that authorize a *paid* intervention -- mrr, plan, tenure, activation, signals --
must therefore not be trusted from the client: otherwise a user opens devtools, posts
`mrr: 99999`, and unlocks the most generous save offer on the menu.

So a customer configured with a `signing_secret` has their BACKEND mint a short-lived token
over the verified user record; the SDK forwards it; we verify the HMAC here and authorize
off the token's claims, never the raw request body. This is the same pattern as Intercom's
identity verification. Kept dependency-free (hmac/hashlib/base64/json) on purpose.

Token = base64url(payload_json) + "." + base64url(hmac_sha256(secret, payload_b64)).
The payload carries the user claims plus `iat` (issued-at, unix seconds); a token older than
`max_age` is rejected, which bounds the replay window on a leaked token.
"""

import base64
import hashlib
import hmac
import json
from typing import Any


class IdentityError(Exception):
    """The identity token was missing, malformed, signed with the wrong secret, or stale."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: str, payload_b64: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64u_encode(mac.digest())


def sign_identity(secret: str, claims: dict[str, Any], iat: int) -> str:
    """Mint a token. A customer runs the equivalent of this in their own backend; we ship it
    so the demo, the tests, and the docs all share one canonical implementation."""
    payload = {**claims, "iat": int(iat)}
    payload_b64 = _b64u_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return payload_b64 + "." + _sign(secret, payload_b64)


def verify_identity(secret: str, token: str, now: int, max_age: int = 600) -> dict[str, Any]:
    """Return the verified claims, or raise IdentityError. Constant-time signature check;
    rejects a token older than `max_age` seconds or minted in the future (beyond skew)."""
    if not token or "." not in token:
        raise IdentityError("malformed identity token")
    payload_b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload_b64)):
        raise IdentityError("identity token signature mismatch")
    try:
        claims = json.loads(_b64u_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise IdentityError("undecodable identity token payload") from exc
    if not isinstance(claims, dict):
        raise IdentityError("identity token payload is not an object")
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        raise IdentityError("identity token missing iat")
    if iat > now + 60:  # small clock-skew tolerance
        raise IdentityError("identity token issued in the future")
    if now - iat > max_age:
        raise IdentityError("identity token expired")
    return claims
