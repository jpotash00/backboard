"""Abuse control on the endpoints that cost a model call.

A publishable key is public by design -- it ships in the browser bundle, so anyone can read
it. The controls that actually stop a lifted key from draining your model budget are (1) the
signed identity token (a lifted key still can't authorize a paid save without the customer's
secret) and (2) this: a per-key and per-IP rate limit.

Two interchangeable backends behind one `check(key, limit, window_seconds)` contract:

  - `RateLimiter`      -- in-memory fixed-window counter. Zero infra, but per-process: with N
                          instances behind a load balancer the effective limit is N x the
                          configured one, so the budget-drain guard weakens exactly as you
                          scale out. Correct only for a single instance.
  - `RedisRateLimiter` -- the same fixed window as a shared Redis INCR+EXPIRE, so every
                          instance draws from ONE counter. This is what keeps the limit real
                          on the multi-instance topology (the same deployments that need the
                          shared session store). Selected when OFFBOARD_REDIS_URL is set.

Nothing else in the request path changes between the two.
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


class RedisRateLimiter:
    """Fixed-window counter backed by a shared Redis, so a limit spans every instance instead
    of resetting per process. Same `check` contract as `RateLimiter` -- `create_app` swaps one
    for the other with no other change.

    Each (key, window) maps to a bucketed Redis key that is INCR'd per hit and given a TTL, so
    it self-expires when the window rolls over (no sweeper needed). INCR is atomic, so two
    instances racing the same key can't both slip past the limit. `now` is injectable so tests
    drive the window deterministically, mirroring the in-memory limiter."""

    def __init__(
        self,
        redis_client,
        now: Callable[[], float] = None,  # defaults to time.time; injected in tests
        prefix: str = "offboard:rl:",
    ) -> None:
        import time

        self._r = redis_client
        self._now = now or time.time
        self._prefix = prefix

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = self._now()
        bucket = int(now // window_seconds)
        redis_key = f"{self._prefix}{key}:{bucket}"
        # INCR then (re)set the TTL in one round trip. Setting EXPIRE on every hit (not just the
        # first) is deliberate: it guarantees the key can never be left without a TTL if a
        # process dies mid-sequence, at the cost of a bucket living at most one extra window --
        # harmless, since a rolled-over bucket is a fresh key the count no longer reads.
        pipe = self._r.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds)
        count = pipe.execute()[0]
        if count > limit:
            elapsed = int(now) % window_seconds
            raise RateLimitExceeded(retry_after=max(1, window_seconds - elapsed))


def redis_ratelimiter_from_url(
    url: str, now: Callable[[], float] = None, prefix: str = "offboard:rl:"
) -> RedisRateLimiter:
    """Build a RedisRateLimiter from a connection URL. `redis` is an optional dependency
    (install `.[redis]`); imported here so the in-memory path needs nothing extra."""
    import redis  # optional dependency, only needed for the shared backend

    return RedisRateLimiter(redis.Redis.from_url(url), now=now, prefix=prefix)
