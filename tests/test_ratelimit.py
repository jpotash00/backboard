"""Rate-limiter backend tests.

The threat these two backends defend against is a lifted publishable key (or a scripted IP)
draining the model budget. The in-memory limiter is correct for one process; the Redis
limiter is what keeps the SAME limit real once you run several instances -- these tests pin
that a shared counter can't be beaten by spreading hits across instances, and that the
proxy-aware client-IP resolution keys the per-IP limit off the real user, not the edge proxy.
"""

import pytest

from api.app import _resolve_client_ip
from api.ratelimit import RateLimiter, RateLimitExceeded, RedisRateLimiter


# --- Shared fake redis (incr/expire via pipeline, matching redis-py's surface) -------------

class _FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        if key in self.counts:
            self.ttl[key] = seconds

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r):
        self._r = r
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, seconds):
        self._ops.append(("expire", key, seconds))
        return self

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "incr":
                out.append(self._r.incr(op[1]))
            else:
                self._r.expire(op[1], op[2])
                out.append(True)
        self._ops = []
        return out


# --- RedisRateLimiter ----------------------------------------------------------------------

def test_redis_limiter_allows_up_to_limit_then_blocks():
    fake = _FakeRedis()
    rl = RedisRateLimiter(fake, now=lambda: 1000.0)
    for _ in range(3):
        rl.check("k", limit=3, window_seconds=60)  # 1,2,3 -- all fine
    with pytest.raises(RateLimitExceeded):
        rl.check("k", limit=3, window_seconds=60)  # 4th trips


def test_redis_limiter_is_shared_across_instances():
    """The whole point of the Redis backend: two app instances hitting the SAME shared redis
    draw from one counter, so the limit can't be beaten by spreading load across processes."""
    fake = _FakeRedis()
    inst_a = RedisRateLimiter(fake, now=lambda: 1000.0)
    inst_b = RedisRateLimiter(fake, now=lambda: 1000.0)
    inst_a.check("session:key:acme", limit=2, window_seconds=60)  # count 1 on A
    inst_b.check("session:key:acme", limit=2, window_seconds=60)  # count 2 on B
    with pytest.raises(RateLimitExceeded):
        inst_a.check("session:key:acme", limit=2, window_seconds=60)  # count 3 -> blocked


def test_redis_limiter_window_rolls_over():
    clock = {"t": 1000.0}
    rl = RedisRateLimiter(fake := _FakeRedis(), now=lambda: clock["t"])
    for _ in range(3):
        rl.check("k", limit=3, window_seconds=60)
    with pytest.raises(RateLimitExceeded):
        rl.check("k", limit=3, window_seconds=60)
    clock["t"] += 60  # next window -> fresh bucketed key, count resets
    rl.check("k", limit=3, window_seconds=60)  # no raise
    assert len(fake.counts) == 2  # two distinct bucket keys existed


def test_redis_limiter_always_sets_ttl():
    """Every hit (re)sets the TTL, so a bucketed key can never be orphaned without expiry."""
    fake = _FakeRedis()
    rl = RedisRateLimiter(fake, now=lambda: 1000.0)
    rl.check("k", limit=10, window_seconds=60)
    assert list(fake.ttl.values()) == [60]


def test_redis_and_memory_agree_on_the_boundary():
    """Same contract: both allow exactly `limit` hits then raise on the next."""
    mem = RateLimiter(now=lambda: 1000.0)
    red = RedisRateLimiter(_FakeRedis(), now=lambda: 1000.0)
    for backend in (mem, red):
        for _ in range(5):
            backend.check("k", limit=5, window_seconds=60)
        with pytest.raises(RateLimitExceeded):
            backend.check("k", limit=5, window_seconds=60)


# --- Proxy-aware client IP (per-IP limit must key off the user, not the edge proxy) --------

def test_no_trust_uses_socket_peer():
    # hops=0 (default): ignore XFF entirely, trust only the direct socket peer.
    assert _resolve_client_ip("10.0.0.1", "1.2.3.4", trusted_proxy_hops=0) == "10.0.0.1"


def test_single_hop_reads_forwarded_client():
    # One edge proxy (e.g. Fly): the real client is the last XFF entry.
    assert _resolve_client_ip("proxy", "203.0.113.7", trusted_proxy_hops=1) == "203.0.113.7"


def test_client_cannot_spoof_by_prepending_entries():
    # A hostile client prepends a fake IP; with 1 trusted hop we index from the RIGHT, so the
    # forged left entry is ignored and the real (proxy-appended) client IP wins.
    forwarded = "9.9.9.9, 203.0.113.7"  # "9.9.9.9" is attacker-controlled
    assert _resolve_client_ip("proxy", forwarded, trusted_proxy_hops=1) == "203.0.113.7"


def test_two_trusted_hops_indexes_further_in():
    forwarded = "203.0.113.7, 10.0.0.9"  # client, inner-proxy; outer proxy is the socket peer
    assert _resolve_client_ip("edge", forwarded, trusted_proxy_hops=2) == "203.0.113.7"


def test_falls_back_to_peer_when_header_too_short():
    # Configured for a proxy but the header is absent/short -> don't collapse onto "", keep peer.
    assert _resolve_client_ip("10.0.0.1", "", trusted_proxy_hops=1) == "10.0.0.1"
    assert _resolve_client_ip("10.0.0.1", "203.0.113.7", trusted_proxy_hops=2) == "10.0.0.1"
