import hashlib

from .errors import AuthError

_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])) end
if current > tonumber(ARGV[1]) then return 0 end
return 1
"""


class AuthRateLimiter:
    def __init__(self, redis, *, prefix: str = "zvonilka:auth:rate:"):
        self.redis, self.prefix = redis, prefix

    async def check(self, category: str, identity: str, limit: int, window: int) -> None:
        # Categories and policy come only from server code, never raw request fields.
        digest = hashlib.sha256(identity.encode()).hexdigest()
        allowed = await self.redis.eval(
            _SCRIPT, 1, self.prefix + category + ":" + digest, str(limit), str(window)
        )
        if not allowed:
            raise AuthError("rate_limited", 429)
