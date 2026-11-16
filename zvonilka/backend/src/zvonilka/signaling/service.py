"""Single-event-loop presence with per-account/room generation fencing."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol
from uuid import UUID, uuid4

from .schemas import Authenticate, ErrorCode, Heartbeat, HeartbeatAck, Joined


class Identity(Protocol):
    @property
    def user_id(self) -> UUID: ...


class Authenticator(Protocol):
    async def authenticate(self, access_token: str) -> Identity: ...


class Rooms(Protocol):
    async def join(self, actor: str, room_id: str, generation: str) -> dict: ...
    async def heartbeat(self, actor: str, room_id: str, generation: str) -> dict: ...
    async def leave(self, actor: str, room_id: str, generation: str) -> dict: ...


@dataclass(frozen=True, slots=True)
class Policy:
    auth_timeout: float = 5.0
    operation_timeout: float = 5.0
    send_timeout: float = 2.0
    cleanup_timeout: float = 3.0
    heartbeat_timeout: float = 30.0
    heartbeat_min_interval: float = 0.25
    shutdown_timeout: float = 10.0
    max_message_bytes: int = 16384
    max_connections: int = 32

    def __post_init__(self) -> None:
        for value in (
            self.auth_timeout,
            self.operation_timeout,
            self.send_timeout,
            self.cleanup_timeout,
            self.heartbeat_timeout,
            self.heartbeat_min_interval,
            self.shutdown_timeout,
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


@dataclass(eq=False, slots=True)
class Connection:
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    reason: ErrorCode | None = None

    def stop(self, reason: ErrorCode) -> None:
        if not self.stopped.is_set():
            self.reason = reason
            self.stopped.set()

    def check(self) -> None:
        if self.stopped.is_set():
            reason = self.reason or "server_shutdown"
            raise Rejected(reason, 1012 if reason == "server_shutdown" else 1008)


@dataclass(slots=True)
class Attempt:
    user_id: UUID
    room_id: UUID
    generation: UUID
    deadline: float
    join_attempted: bool = False
    connection: Connection = field(default_factory=Connection)
    next_heartbeat_at: float = 0.0


@dataclass(slots=True)
class Gate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current: Attempt | None = None
    users: int = 0


def parse_authentication(raw: str) -> Authenticate:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            version = data.get("version")
            if type(version) is int and version != 1:
                raise Rejected("unsupported_version")
        return Authenticate.model_validate(data)
    except (ValueError, RecursionError):
        raise Rejected("invalid_message") from None


def parse_heartbeat(raw: str) -> Heartbeat:
    try:
        return Heartbeat.model_validate_json(raw)
    except (ValueError, RecursionError):
        raise Rejected("invalid_message") from None


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
        self.heartbeat_window = min(self.policy.heartbeat_timeout, presence_ttl / 2)
        if self.heartbeat_window < 0.01:
            raise ValueError("presence window is too short")
        self.heartbeat_interval = min(10.0, self.heartbeat_window / 3)
        self.heartbeat_min_interval = min(
            self.policy.heartbeat_min_interval, self.heartbeat_window / 4
        )
        self.connections = 0
        self.closing = False
        self._handlers: set[Connection] = set()
        self._gates: dict[tuple[UUID, UUID], Gate] = {}

    def acquire(self) -> bool:
        # Synchronous operations: one event loop, never multiple API workers.
        if self.closing or self.connections >= self.policy.max_connections:
            return False
        self.connections += 1
        return True

    def track(self) -> Connection:
        connection = Connection()
        self._handlers.add(connection)
        return connection

    def release(self, connection: Connection | None = None) -> None:
        if connection is not None:
            self._handlers.discard(connection)
            connection.done.set()
        self.connections -= 1

    @property
    def registry_size(self) -> int:
        return len(self._gates)

    @asynccontextmanager
    async def gate(self, attempt: Attempt) -> AsyncIterator[Gate]:
        key = (attempt.user_id, attempt.room_id)
        gate = self._gates.setdefault(key, Gate())
        gate.users += 1
        try:
            async with gate.lock:
                yield gate
        finally:
            gate.users -= 1
            if gate.users == 0 and gate.current is None:
                self._gates.pop(key, None)

    def forget(self, attempt: Attempt) -> None:
        key = (attempt.user_id, attempt.room_id)
        gate = self._gates.get(key)
        if gate is not None:
            if gate.current is attempt:
                gate.current = None
            if gate.users == 0 and gate.current is None:
                self._gates.pop(key, None)

    async def identify(self, message: Authenticate) -> Attempt:
        principal = await self.auth.authenticate(message.access_token.get_secret_value())
        return Attempt(
            principal.user_id, message.room_id, uuid4(), monotonic() + self.heartbeat_window
        )

    async def join(self, attempt: Attempt, request_id: UUID) -> Joined:
        async with self.gate(attempt) as gate:
            attempt.connection.check()
            if self.closing:
                raise Rejected("server_shutdown", 1012)
            started = monotonic()
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
                    "heartbeat_interval_ms": max(1, int(self.heartbeat_interval * 1000)),
                    "heartbeat_timeout_ms": max(2, int(self.heartbeat_window * 1000)),
                }
            )
            if response.room.room_id != attempt.room_id:
                raise RuntimeError("repository returned a different room")
            attempt.deadline = started + self.heartbeat_window
            attempt.next_heartbeat_at = started + self.heartbeat_min_interval
            attempt.connection.check()
            if monotonic() >= attempt.deadline:
                raise Rejected("dependency_unavailable", 1013)
            previous = gate.current
            gate.current = attempt
            if previous is not None and previous is not attempt:
                previous.connection.stop("connection_replaced")
            return response

    async def heartbeat(self, attempt: Attempt, message: Heartbeat) -> HeartbeatAck:
        if message.generation != attempt.generation:
            raise Rejected("stale_generation")
        async with self.gate(attempt) as gate:
            attempt.connection.check()
            if gate.current is not attempt:
                raise Rejected("stale_generation")
            started = monotonic()
            if started >= attempt.deadline:
                raise Rejected("heartbeat_timeout")
            if started < attempt.next_heartbeat_at:
                raise Rejected("rate_limited")
            await self.rooms.heartbeat(
                str(attempt.user_id), str(attempt.room_id), str(attempt.generation)
            )
            attempt.connection.check()
            deadline = started + self.heartbeat_window
            if monotonic() >= deadline:
                raise Rejected("dependency_unavailable", 1013)
            attempt.deadline = deadline
            attempt.next_heartbeat_at = started + self.heartbeat_min_interval
            return HeartbeatAck(
                version=1,
                type="heartbeat_ack",
                request_id=message.request_id,
                generation=attempt.generation,
            )

    async def leave(self, attempt: Attempt) -> None:
        try:
            if attempt.join_attempted:
                await self.rooms.leave(
                    str(attempt.user_id), str(attempt.room_id), str(attempt.generation)
                )
        finally:
            # Generation identity also fences LOCAL cleanup, even if Redis is unavailable.
            self.forget(attempt)

    async def shutdown(self) -> None:
        self.closing = True
        handlers = tuple(self._handlers)
        for connection in handlers:
            connection.stop("server_shutdown")
        if handlers:
            async with asyncio.timeout(self.policy.shutdown_timeout):
                await asyncio.gather(*(connection.done.wait() for connection in handlers))
