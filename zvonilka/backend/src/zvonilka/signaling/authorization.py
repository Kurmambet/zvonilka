"""Pure timing/binding rules. No JWT verification, database, network or background tasks."""

import math
from typing import Literal
from uuid import UUID

AuthorizationState = Literal["authorized", "reauth_required", "expired"]


class AuthorizationRejected(Exception):
    def __init__(self, code: Literal["invalid_access", "reauthentication_timeout"]):
        self.code = code
        super().__init__(code)


def finite(value: float) -> None:
    if type(value) not in (int, float):
        raise TypeError("time must be numeric")
    if not math.isfinite(value):
        raise ValueError("time must be finite")


class AuthorizationWindow:
    """Construct/update ONLY from AuthService-verified identity and exp, not client claims."""

    def __init__(
        self,
        user_id: UUID,
        session_id: UUID,
        expires_at: int,
        *,
        now_wall: float,
        now: float,
        grace_seconds: float = 30.0,
        check_interval: float = 10.0,
    ):
        for value in (now_wall, now, grace_seconds, check_interval):
            finite(value)
        if grace_seconds <= 0 or check_interval <= 0:
            raise ValueError("authorization intervals must be positive")
        self._validate_expiry(expires_at, now_wall)
        self.user_id, self.session_id = user_id, session_id
        self.expires_at = expires_at
        self.grace_seconds, self.check_interval = grace_seconds, check_interval
        # Convert verified wall-clock exp once; runtime waits use monotonic time.
        self._expires_at = now + (expires_at - now_wall)
        self._check_at = now + check_interval
        self._last_now = now

    @staticmethod
    def _validate_expiry(expires_at: int, now_wall: float) -> None:
        if type(expires_at) is not int:
            raise TypeError("verified expiry must be an integer")
        if expires_at <= now_wall:
            raise AuthorizationRejected("invalid_access")

    def _observe(self, now: float) -> None:
        finite(now)
        if now < self._last_now:
            raise ValueError("monotonic time went backwards")
        self._last_now = now

    def state(self, now: float) -> AuthorizationState:
        self._observe(now)
        if now >= self._expires_at + self.grace_seconds:
            return "expired"
        if now >= self._expires_at:
            return "reauth_required"
        return "authorized"

    def session_check_due(self, now: float) -> bool:
        return self.state(now) != "expired" and now >= self._check_at

    def session_checked(self, now: float) -> None:
        # A successful DB check never extends JWT exp or its grace window.
        if self.state(now) == "expired":
            raise AuthorizationRejected("reauthentication_timeout")
        self._check_at = now + self.check_interval

    def reauthenticated(
        self,
        user_id: UUID,
        session_id: UUID,
        expires_at: int,
        *,
        now_wall: float,
        now: float,
    ) -> None:
        if self.state(now) == "expired":
            raise AuthorizationRejected("reauthentication_timeout")
        if (user_id, session_id) != (self.user_id, self.session_id):
            raise AuthorizationRejected("invalid_access")
        finite(now_wall)
        self._validate_expiry(expires_at, now_wall)
        if expires_at < self.expires_at:
            raise AuthorizationRejected("invalid_access")
        deadline = now + (expires_at - now_wall)
        if expires_at == self.expires_at:
            # Replaying the same exp must not gain time after a wall-clock adjustment.
            deadline = min(deadline, self._expires_at)
        if deadline <= now:
            raise AuthorizationRejected("invalid_access")
        self.expires_at, self._expires_at = expires_at, deadline
        self._check_at = now + self.check_interval

    def next_deadline(self, now: float) -> float:
        state = self.state(now)
        if state == "expired":
            return now
        boundary = self._expires_at
        if state == "reauth_required":
            boundary += self.grace_seconds
        return max(now, min(boundary, self._check_at))
