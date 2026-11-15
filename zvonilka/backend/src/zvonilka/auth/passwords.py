from functools import partial
from typing import Protocol

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type

from .identity import validate_password


class Hasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, encoded: str, password: str) -> bool: ...
    def check_needs_rehash(self, encoded: str) -> bool: ...


class Passwords:
    """Create within an async context. Corrupt hashes are server errors, not bad passwords."""

    def __init__(
        self,
        *,
        memory_cost: int = 65536,
        time_cost: int = 3,
        parallelism: int = 1,
        concurrency: int = 2,
    ):
        self.hasher: Hasher = PasswordHasher(
            memory_cost=memory_cost, time_cost=time_cost, parallelism=parallelism, type=Type.ID
        )
        self.limiter = anyio.CapacityLimiter(concurrency)
        self.dummy_hash: str | None = None

    async def hash(self, password: str) -> str:
        validate_password(password)
        return await anyio.to_thread.run_sync(self.hasher.hash, password, limiter=self.limiter)

    async def initialize(self) -> None:
        self.dummy_hash = await self.hash("dummy-password-not-an-account")

    async def verify(self, encoded: str | None, password: str) -> bool:
        target = encoded or self.dummy_hash
        if target is None:
            raise RuntimeError("password service is not initialized")

        def check() -> bool:
            try:
                return self.hasher.verify(target, password)
            except VerifyMismatchError:
                return False

        valid = await anyio.to_thread.run_sync(check, limiter=self.limiter)
        return valid and encoded is not None

    async def needs_rehash(self, encoded: str) -> bool:
        return await anyio.to_thread.run_sync(
            partial(self.hasher.check_needs_rehash, encoded), limiter=self.limiter
        )
