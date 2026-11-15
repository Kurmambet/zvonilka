"""Single-process, short-lived WS admission; no heartbeat or peer signaling yet."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID, uuid4

from .schemas import Authenticate, ErrorCode, Joined


class Identity(Protocol):
    @property
    def user_id(self) -> UUID: ...


class Authenticator(Protocol):
    async def authenticate(self, access_token: str) -> Identity: ...


class Rooms(Protocol):
    async def join(self, actor: str, room_id: str, generation: str) -> dict: ...
    async def leave(self, actor: str, room_id: str, generation: str) -> dict: ...


@dataclass(frozen=True, slots=True)
class Policy:
    auth_timeout: float = 5.0
    operation_timeout: float = 5.0
    send_timeout: float = 2.0
    cleanup_timeout: float = 3.0
    session_seconds: float = 20.0
    max_message_bytes: int = 16384
    max_connections: int = 32

    def __post_init__(self) -> None:
        for value in (
            self.auth_timeout,
            self.operation_timeout,
            self.send_timeout,
            self.cleanup_timeout,
            self.session_seconds,
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be finite and positive")
        for value in (self.max_message_bytes, self.max_connections):
            if type(value) is not int or value < 1:
                raise ValueError("limits must be positive integers")


class Rejected(Exception):
    code: ErrorCode
    close_code: int

    def __init__(self, code: ErrorCode, close_code: int = 1008):
        self.code = code
        self.close_code = close_code
        super().__init__(code)


@dataclass(slots=True)
class Attempt:
    user_id: UUID
    room_id: UUID
    generation: UUID
    deadline: float
    join_attempted: bool = False


def parse_authentication(raw: str) -> Authenticate:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            version = data.get("version")
            if type(version) is int and version != 1:
                raise Rejected("unsupported_version")
        message = Authenticate.model_validate(data)
    except (ValueError, RecursionError):
        raise Rejected("invalid_message") from None
    return message


class SignalingService:
    def __init__(
        self,
        auth: Authenticator,
        rooms: Rooms,
        *,
        presence_ttl: float,
        policy: Policy | None = None,
    ):
        if not math.isfinite(presence_ttl) or presence_ttl <= 0:
            raise ValueError("presence TTL must be finite and positive")
        self.auth = auth
        self.rooms = rooms
        self.policy = policy or Policy()
        self.session_seconds = min(self.policy.session_seconds, presence_ttl / 2)
        self.connections = 0

    def acquire(self) -> bool:
        # No await: atomic with respect to this one event loop, not multiple workers.
        if self.connections >= self.policy.max_connections:
            return False
        self.connections += 1
        return True

    def release(self) -> None:
        self.connections -= 1

    async def identify(self, message: Authenticate) -> Attempt:
        principal = await self.auth.authenticate(message.access_token.get_secret_value())
        return Attempt(
            principal.user_id, message.room_id, uuid4(), monotonic() + self.session_seconds
        )

    async def join(self, attempt: Attempt, request_id: UUID) -> Joined:
        # Set BEFORE awaiting EVAL: cancellation/connection loss may hide a committed join.
        attempt.join_attempted = True
        result = await self.rooms.join(
            str(attempt.user_id), str(attempt.room_id), str(attempt.generation)
        )
        response = Joined.model_validate(
            {
                "version": 1,
                "type": "joined",
                "request_id": request_id,
                "user_id": attempt.user_id,
                "generation": attempt.generation,
                "room": result["room"],
                "capabilities": [],
            }
        )
        if response.room.room_id != attempt.room_id:
            raise RuntimeError("repository returned a different room")
        return response

    async def leave(self, attempt: Attempt) -> None:
        if attempt.join_attempted:
            await self.rooms.leave(
                str(attempt.user_id), str(attempt.room_id), str(attempt.generation)
            )
