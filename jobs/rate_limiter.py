import time
import redis
from django.conf import settings

_LUA_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window)
    return 1
else
    return 0
end
"""


class RateLimitExceeded(Exception):
    """Raised when the caller should back off; carries a suggested retry delay."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded, retry after {retry_after:.2f}s")


class SlidingWindowRateLimiter:
    def __init__(self, redis_client=None, key="email:rate_limit", limit=200, window_seconds=60):
        self.redis = redis_client or redis.Redis.from_url(settings.REDIS_URL)
        self.key = key
        self.limit = limit
        self.window = window_seconds
        self._script = self.redis.register_script(_LUA_SLIDING_WINDOW)

    def try_acquire(self) -> bool:
        """
        Attempt to record one call. Returns True if allowed, False if the
        caller must back off. Fails OPEN on Redis errors -- see DESIGN.md
        for why.
        """
        now = time.time()
        member = f"{now}:{id(object())}"
        self.last_acquire_time = now
        try:
            allowed = self._script(keys=[self.key], args=[now, self.window, self.limit, member])
            return bool(allowed)
        except redis.RedisError:
            return True

    def current_count(self) -> int:
        now = time.time()
        self.redis.zremrangebyscore(self.key, "-inf", now - self.window)
        return self.redis.zcard(self.key)