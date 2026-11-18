"""Single-event-loop presence with per-account/room generation fencing."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic, time
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter

from .auth_protocol import Reauthenticate, Reauthenticated
from .authorization import AuthorizationRejected, AuthorizationWindow
from .participants import ParticipantsHub
from .schemas import (
    Answer,
    Authenticate,
    Error,
    ErrorCode,
    Heartbeat,
    HeartbeatAck,
    IceCandidate,
    Joined,
    Offer,
    RelayedAnswer,
    RelayedIceCandidate,
    RelayedMessage,
    RelayedOffer,
    RelayMessage,
)


class Principal(Protocol):
    @property
    def user_id(self) -> UUID: ...

    @property
    def session_id(self) -> UUID: ...


class Access(Protocol):
    @property
    def principal(self) -> Principal: ...

    @property
    def expires_at(self) -> int: ...


class Authenticator(Protocol):
    # Must verify JWT signature/exp AND the server-side session before returning.
    async def authenticate_access(self, access_token: str) -> Access: ...
    async def check_session(self, user_id: UUID, session_id: UUID) -> Principal: ...


class Rooms(Protocol):
    async def join(self, actor: str, room_id: str, generation: str) -> dict: ...
    async def heartbeat(self, actor: str, room_id: str, generation: str) -> dict: ...
    async def leave(self, actor: str, room_id: str, generation: str) -> dict: ...


@dataclass(frozen=True, slots=True)
class Policy:
    auth_timeout: float = 5.0
    session_check_interval: float = 10.0
    operation_timeout: float = 5.0
    send_timeout: float = 2.0
    cleanup_timeout: float = 3.0
    heartbeat_timeout: float = 30.0
    heartbeat_min_interval: float = 0.25
    shutdown_timeout: float = 10.0
    max_message_bytes: int = 16384
    max_connections: int = 32
    # Allow ICE bursts while bounding sustained signaling independently of heartbeat.
    relay_rate: int = 32
    relay_burst: int = 64
    # Each reauthenticate attempt costs a PostgreSQL session check.
    reauth_min_interval: float = 1.0
    reauth_grace_seconds: float = 30.0

    def __post_init__(self) -> None:
        for value in (
            self.auth_timeout,
            self.session_check_interval,
            self.operation_timeout,
            self.send_timeout,
            self.cleanup_timeout,
            self.heartbeat_timeout,
            self.heartbeat_min_interval,
            self.shutdown_timeout,
            self.reauth_min_interval,
            self.reauth_grace_seconds,
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be finite and positive")
        for value in (
            self.max_message_bytes,
            self.max_connections,
            self.relay_rate,
            self.relay_burst,
        ):
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
            if reason == "dependency_unavailable":
                raise Rejected(reason, 1013)
            raise Rejected(reason, 1012 if reason == "server_shutdown" else 1008)


@dataclass(slots=True)
class Attempt:
    user_id: UUID
    room_id: UUID
    generation: UUID
    deadline: float
    # Authorization of the existing WS; separate from the presence lease above.
    authorization: AuthorizationWindow
    join_attempted: bool = False
    connection: Connection = field(default_factory=Connection)
    next_heartbeat_at: float = 0.0
    relay_tokens: float = 0.0
    relay_updated_at: float | None = None
    next_reauth_at: float = 0.0


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


SessionMessage = Heartbeat | Offer | Answer | IceCandidate | Reauthenticate
session_message_adapter = TypeAdapter(Annotated[SessionMessage, Field(discriminator="type")])


def parse_active(raw: str) -> SessionMessage:
    try:
        return session_message_adapter.validate_json(raw)
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
        self.participants = ParticipantsHub()
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
        access = await self.auth.authenticate_access(message.access_token.get_secret_value())
        now = monotonic()
        try:
            window = AuthorizationWindow(
                access.principal.user_id,
                access.principal.session_id,
                access.expires_at,
                now_wall=time(),
                now=now,
                grace_seconds=self.policy.reauth_grace_seconds,
            )
        except AuthorizationRejected:
            raise Rejected("invalid_access") from None
        return Attempt(
            access.principal.user_id,
            message.room_id,
            uuid4(),
            now + self.heartbeat_window,
            window,
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

    async def reauthenticate(self, attempt: Attempt, message: Reauthenticate) -> Reauthenticated:
        # Replaces only WS authorization. Generation, lease and Redis stay untouched.
        attempt.connection.check()
        if self.closing:
            raise Rejected("server_shutdown", 1012)
        if message.generation != attempt.generation:
            raise Rejected("stale_generation")
        started = monotonic()
        if started >= attempt.deadline:
            raise Rejected("heartbeat_timeout")
        if not self.is_current(attempt):
            raise Rejected("stale_generation")
        if started < attempt.next_reauth_at:
            raise Rejected("rate_limited")
        # Failed attempts count too: every call is a database check.
        attempt.next_reauth_at = started + self.policy.reauth_min_interval
        access = await self.auth.authenticate_access(message.access_token.get_secret_value())
        # The awaited check may have crossed replacement, shutdown or lease expiry.
        attempt.connection.check()
        if not self.is_current(attempt):
            raise Rejected("stale_generation")
        try:
            attempt.authorization.reauthenticated(
                access.principal.user_id,
                access.principal.session_id,
                access.expires_at,
                now_wall=time(),
                now=monotonic(),
            )
        except AuthorizationRejected as exc:
            raise Rejected(exc.code) from None
        return Reauthenticated(
            version=1,
            type="reauthenticated",
            request_id=message.request_id,
            generation=attempt.generation,
            expires_at=attempt.authorization.expires_at,
        )

    def is_current(self, attempt: Attempt) -> bool:
        gate = self._gates.get((attempt.user_id, attempt.room_id))
        return (
            not self.closing
            and not attempt.connection.stopped.is_set()
            and gate is not None
            and gate.current is attempt
            and monotonic() < attempt.deadline
        )

    def can_receive_relay(self, attempt: Attempt) -> bool:
        return self.is_current(attempt) and attempt.authorization.state(monotonic()) == "authorized"

    def relay(self, attempt: Attempt, message: RelayMessage) -> Error | None:
        attempt.connection.check()
        if self.closing:
            raise Rejected("server_shutdown", 1012)
        if message.generation != attempt.generation:
            raise Rejected("stale_generation")
        if monotonic() >= attempt.deadline:
            raise Rejected("heartbeat_timeout")
        if not self.is_current(attempt):
            raise Rejected("stale_generation")
        now = monotonic()
        if attempt.relay_updated_at is None:
            attempt.relay_tokens = float(self.policy.relay_burst)
        else:
            attempt.relay_tokens = min(
                float(self.policy.relay_burst),
                attempt.relay_tokens + (now - attempt.relay_updated_at) * self.policy.relay_rate,
            )
        attempt.relay_updated_at = now
        if attempt.relay_tokens < 1:
            raise Rejected("rate_limited")
        attempt.relay_tokens -= 1
        state = attempt.authorization.state(now)
        if state == "expired":
            raise Rejected("reauthentication_timeout")
        if state == "reauth_required":
            return Error(
                version=1,
                type="error",
                request_id=message.request_id,
                code="authorization_expired",
                fatal=False,
            )
        common = {
            "version": 1,
            "type": message.type,
            "request_id": message.request_id,
            "room_id": attempt.room_id,
            "from_user_id": attempt.user_id,
            "from_generation": attempt.generation,
            "target_generation": message.target_generation,
        }
        envelope: RelayedMessage
        if isinstance(message, Offer):
            envelope = RelayedOffer.model_validate({**common, "sdp": message.sdp})
        elif isinstance(message, Answer):
            envelope = RelayedAnswer.model_validate({**common, "sdp": message.sdp})
        elif isinstance(message, IceCandidate):
            envelope = RelayedIceCandidate.model_validate(
                {**common, "candidate": message.candidate}
            )
        else:
            raise TypeError("unsupported relay message")
        payload = envelope.model_dump_json()
        # The server envelope adds metadata; check its byte size before touching the target queue.
        if len(payload.encode("utf-8")) > self.policy.max_message_bytes:
            return Error(
                version=1,
                type="error",
                request_id=message.request_id,
                code="message_too_large",
                fatal=False,
            )
        result = self.participants.deliver(
            attempt.room_id,
            attempt.user_id,
            attempt.generation,
            message.target_user_id,
            message.target_generation,
            payload,
        )
        if result == "sender_stale":
            raise Rejected("stale_generation")
        if result != "sent":
            return Error(
                version=1, type="error", request_id=message.request_id, code=result, fatal=False
            )
        # Signaling never renews the presence lease; heartbeat remains mandatory.
        return None

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
