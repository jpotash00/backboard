"""Abuse control on the endpoints that cost a model call.

A publishable key is public by design -- it ships in the browser bundle, so anyone can read
it. The controls that actually stop a lifted key from draining your model budget are (1) the
signed identity token (a lifted key still can't authorize a paid save without the customer's
secret) and (2) this: a per-key and per-IP rate limit.

In-memory fixed-window counter for Milestone 2. `check` is backend-agnostic so a Redis
INCR+EXPIRE implementation drops straight in for multi-process deployments; nothing else in
the request path changes.
"""

from typing import Callable

_MAX_TRACKED = 100_000  # backstop: prune if distinct keys ever blow past this


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = retry_after


class RateLimiter:
    """Fixed-window counter. Each `key` gets `limit` hits per `window_seconds`; the (window,
    count) pair is stored per key and reset when the window rolls over."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._state: dict[str, tuple[int, int]] = {}

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = self._now()
        bucket = int(now // window_seconds)
        cur_bucket, count = self._state.get(key, (bucket, 0))
        if cur_bucket != bucket:
            cur_bucket, count = bucket, 0
        count += 1
        self._state[key] = (cur_bucket, count)
        if len(self._state) > _MAX_TRACKED:
            self._prune(bucket)
        if count > limit:
            elapsed = int(now) % window_seconds
            raise RateLimitExceeded(retry_after=max(1, window_seconds - elapsed))

    def _prune(self, current_bucket: int) -> None:
        """Drop keys whose window has already rolled over -- they'd reset on next hit anyway."""
        self._state = {
            k: v for k, v in self._state.items() if v[0] == current_bucket
        }
