# Project Structure (selected)

## Directory Tree

```
./
└── zvonilka/
    └── backend/
        ├── src/
        │   └── zvonilka/
        │       ├── signaling/
        │       │   ├── router.py
        │       │   ├── schemas.py
        │       │   └── service.py
        │       └── api.py
        └── tests/
            ├── signaling_test_support.py
            ├── test_signaling_integration.py
            ├── test_signaling_network.py
            ├── test_signaling_schemas.py
            ├── test_signaling_service.py
            └── test_signaling_transport.py
```

---

# File Contents

## zvonilka/backend/src/zvonilka/api.py

```python
import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException

from .auth.errors import AuthError
from .auth.jwt_tokens import AccessTokens
from .auth.passwords import Passwords
from .auth.rate_limit import AuthRateLimiter
from .auth.schemas import (
    ChangePasswordInput,
    LoginInput,
    Profile,
    RefreshInput,
    RegisterInput,
    SessionView,
    Tokens,
)
from .auth.service import AuthService, Principal
from .config import Settings
from .db import create_database
from .http_boundary import SafeHttpBoundary
from .observability.logging import log_event
from .rooms.http_errors import room_http_error
from .rooms.repository import RoomError, RoomRepository
from .rooms.router import room_router
from .rooms.service import RoomService
from .signaling.router import signaling_router
from .signaling.service import SignalingService

bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None):
    # Required values are loaded and validated from environment/.env.
    settings = settings or Settings()  # pyright: ignore[reportCallIssue]

    @asynccontextmanager
    async def lifespan(app):
        engine, sessions = create_database(settings)
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
        started = False
        try:
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            except Exception:
                import sys

                print(
                    "STARTUP_ERROR postgres_unavailable: check database connection and permissions",
                    file=sys.stderr,
                )
                raise
            try:
                await redis.ping()
            except Exception:
                import sys

                print(
                    "STARTUP_ERROR redis_unavailable: check Redis connection and credentials",
                    file=sys.stderr,
                )
                raise
            passwords = Passwords()
            await passwords.initialize()
            app.state.auth = AuthService(
                sessions,
                passwords,
                AccessTokens(
                    settings.jwt_secret.get_secret_value(),
                    settings.jwt_issuer,
                    settings.jwt_audience,
                    settings.access_ttl_seconds,
                ),
                refresh_ttl=settings.refresh_ttl_seconds,
                max_sessions=settings.max_sessions_per_user,
                max_refresh_tokens=settings.max_refresh_tokens_per_session,
            )
            app.state.limiter = AuthRateLimiter(redis, prefix=settings.rate_limit_prefix)
            app.state.engine, app.state.redis = engine, redis
            app.state.settings = settings
            app.state.room_repository = RoomRepository(
                redis, limits=settings.room_limits(), key=settings.room_registry_key
            )
            app.state.rooms = RoomService(app.state.room_repository)
            app.state.signaling = SignalingService(
                app.state.auth,
                app.state.room_repository,
                presence_ttl=settings.room_presence_ttl_seconds,
            )
            started = True
            log_event(logging.getLogger("zvonilka.api"), "service_started")
            yield
        finally:
            if started:
                log_event(logging.getLogger("zvonilka.api"), "service_stopped")
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Zvonilka", version="0.5.0.dev0", lifespan=lifespan, debug=False)

    def error(request, status, code):
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
        return JSONResponse(
            status_code=status,
            headers=headers,
            content={"error": {"code": code, "request_id": request.state.request_id}},
        )

    @app.exception_handler(Exception)
    async def unknown_error(request: Request, exc: Exception):
        return error(request, 500, "internal_error")

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return error(request, exc.status, exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never serialize exc.errors(): it may contain passwords and tokens.
        return error(request, 422, "invalid_request")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error(
            request,
            exc.status_code,
            {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "request_rejected"),
        )

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError):
        # Details are deliberately not returned or interpolated into logs.
        log_event(
            logging.getLogger("zvonilka.api"),
            "dependency_unavailable",
            level=logging.ERROR,
            code="postgres_unavailable",
            dependency="postgres",
            request_id=request.state.request_id,
            exc_info=True,
        )
        return error(request, 503, "database_unavailable")

    @app.exception_handler(RedisError)
    async def redis_error(request: Request, exc: RedisError):
        log_event(
            logging.getLogger("zvonilka.api"),
            "dependency_unavailable",
            level=logging.ERROR,
            code="redis_unavailable",
            dependency="redis",
            request_id=request.state.request_id,
            exc_info=True,
        )
        return error(request, 503, "redis_unavailable")

    @app.exception_handler(RoomError)
    async def room_error(request: Request, exc: RoomError):
        status, code = room_http_error(exc.code)
        if status >= 500:
            log_event(
                logging.getLogger("zvonilka.api"),
                "unexpected_error" if status == 500 else "dependency_unavailable",
                level=logging.ERROR,
                code=code,
                dependency="redis",
                request_id=request.state.request_id,
                exc_info=True,
            )
        return error(request, status, code)

    async def principal(
        request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthError("invalid_access")
        return await request.app.state.auth.authenticate(credentials.credentials)

    async def rate(
        request: Request, category: str, limit: int, window: int, identity: str | None = None
    ):
        # Do not trust X-Forwarded-For. Deployment must define trusted proxies explicitly.
        identity = identity or (request.client.host if request.client else "unknown")
        await request.app.state.limiter.check(category, identity, limit, window)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await request.app.state.redis.ping()
        return {"status": "ready"}

    @app.post("/api/v1/auth/register", response_model=Profile, status_code=201)
    async def register(body: RegisterInput, request: Request):
        await rate(request, "register_ip", 5, 3600)
        return await request.app.state.auth.register(
            body.username, body.display_name, body.password.get_secret_value()
        )

    @app.post("/api/v1/auth/login", response_model=Tokens)
    async def login(body: LoginInput, request: Request):
        await rate(request, "login_ip", 60, 60)
        await rate(request, "login_username", 12, 300, body.username)
        return await request.app.state.auth.login(body.username, body.password.get_secret_value())

    @app.post("/api/v1/auth/refresh", response_model=Tokens)
    async def refresh(body: RefreshInput, request: Request):
        await rate(request, "refresh_ip", 60, 60)
        return await request.app.state.auth.refresh(body.refresh_token.get_secret_value())

    @app.get("/api/v1/users/me", response_model=Profile)
    async def me(user: Principal = Depends(principal)):
        return user.profile()

    @app.get("/api/v1/auth/sessions", response_model=list[SessionView])
    async def list_sessions(request: Request, user: Principal = Depends(principal)):
        return await request.app.state.auth.list_sessions(user)

    @app.delete("/api/v1/auth/sessions/{session_id}", status_code=204)
    async def revoke(session_id: UUID, request: Request, user: Principal = Depends(principal)):
        await rate(request, "revoke_user", 60, 60, str(user.user_id))
        await request.app.state.auth.revoke(user, session_id)
        return Response(status_code=204)

    @app.post("/api/v1/auth/logout", status_code=204)
    async def logout(request: Request, user: Principal = Depends(principal)):
        await request.app.state.auth.revoke(user, user.session_id)
        return Response(status_code=204)

    @app.post("/api/v1/auth/password", status_code=204)
    async def password(
        body: ChangePasswordInput, request: Request, user: Principal = Depends(principal)
    ):
        await rate(request, "password_user", 5, 300, str(user.user_id))
        await request.app.state.auth.change_password(
            user, body.current_password.get_secret_value(), body.new_password.get_secret_value()
        )
        return Response(status_code=204)

    app.include_router(room_router(principal, rate))
    app.include_router(signaling_router())

    # Wrapper is outside Starlette ServerErrorMiddleware: unknown failures cannot
    # leak its plain-text traceback. That middleware may already send generic 500.
    return SafeHttpBoundary(app, max_body=settings.auth_body_limit_bytes)

```


## zvonilka/backend/src/zvonilka/signaling/router.py

```python
"""Bounded admission transport. This delivery intentionally has no heartbeat."""

import asyncio
import logging
from time import monotonic
from uuid import UUID

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError

from zvonilka.auth.errors import AuthError
from zvonilka.observability.logging import log_event
from zvonilka.rooms.repository import RoomError

from .schemas import Error, ErrorCode
from .service import Attempt, Rejected, SignalingService, parse_authentication

logger = logging.getLogger("zvonilka.signaling")
ROOM_ERRORS: dict[str, ErrorCode] = {
    "room_not_found": "room_not_found",
    "room_closed": "room_closed",
    "not_admitted": "not_admitted",
    "room_limit": "room_limit",
    "owner_room_limit": "owner_room_limit",
    "participant_limit": "participant_limit",
    "overloaded": "overloaded",
    "registry_full": "registry_full",
    "stale_generation": "stale_generation",
    "schema_mismatch": "dependency_unavailable",
}


async def receive_text(websocket: WebSocket, limit: int) -> str:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    raw = message.get("text")
    if not isinstance(raw, str):
        raise Rejected("invalid_message", 1003)
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeError:
        raise Rejected("invalid_message") from None
    if size > limit:
        raise Rejected("message_too_large", 1009)
    return raw


async def close_socket(websocket: WebSocket, timeout: float, code: int) -> None:
    try:
        with anyio.CancelScope(shield=True):
            async with asyncio.timeout(timeout):
                await websocket.close(code=code)
    except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
        pass


async def send_error(
    websocket: WebSocket,
    service: SignalingService,
    code: ErrorCode,
    request_id: UUID | None,
    close_code: int,
) -> None:
    response = Error(version=1, type="error", request_id=request_id, code=code, fatal=True)
    try:
        async with asyncio.timeout(service.policy.send_timeout):
            await websocket.send_text(response.model_dump_json())
    except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
        pass
    await close_socket(websocket, service.policy.send_timeout, close_code)


async def cleanup(service: SignalingService, attempt: Attempt | None) -> None:
    if attempt is None or not attempt.join_attempted:
        return
    try:
        with anyio.CancelScope(shield=True):
            async with asyncio.timeout(service.policy.cleanup_timeout):
                await service.leave(attempt)
    except RoomError as exc:
        if exc.code not in {"stale_generation", "room_closed", "room_not_found"}:
            log_event(logger, "unexpected_error", code="internal_error", operation="leave")
    except (RedisError, TimeoutError):
        log_event(
            logger,
            "dependency_unavailable",
            code="redis_unavailable",
            dependency="redis",
            operation="leave",
        )
    except Exception:  # noqa: BLE001 -- Cleanup boundary; never serialize exception text.
        log_event(logger, "unexpected_error", code="internal_error", operation="leave")


def signaling_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["signaling"])

    @router.websocket("/rooms/{room_id}/ws")
    async def connect(websocket: WebSocket, room_id: str) -> None:
        service: SignalingService = websocket.app.state.signaling
        try:
            target = UUID(room_id)
        except ValueError:
            await close_socket(websocket, service.policy.send_timeout, 1008)
            return
        # No query parameters: tokens must never become URL/access-log data.
        if websocket.scope.get("query_string"):
            await close_socket(websocket, service.policy.send_timeout, 1008)
            return
        if not service.acquire():
            await close_socket(websocket, service.policy.send_timeout, 1013)
            return
        attempt: Attempt | None = None
        request_id: UUID | None = None
        accepted = False
        cancelled = False
        operation_dependency = "postgres"
        try:
            async with asyncio.timeout(service.policy.send_timeout):
                await websocket.accept()
            accepted = True
            try:
                async with asyncio.timeout(service.policy.auth_timeout):
                    raw = await receive_text(websocket, service.policy.max_message_bytes)
            except TimeoutError:
                raise Rejected("authentication_timeout") from None
            message = parse_authentication(raw)
            request_id = message.request_id
            if message.room_id != target:
                raise Rejected("invalid_message")
            del raw
            async with asyncio.timeout(service.policy.operation_timeout):
                attempt = await service.identify(message)
                del message
                operation_dependency = "redis"
                response = await service.join(attempt, request_id)
            remaining = attempt.deadline - monotonic()
            if remaining <= 0:
                raise Rejected("dependency_unavailable", 1013)
            async with asyncio.timeout(min(service.policy.send_timeout, remaining)):
                await websocket.send_text(response.model_dump_json())
            # Until heartbeat is added, never leave a socket open past a safe lease window.
            remaining = attempt.deadline - monotonic()
            if remaining > 0:
                try:
                    async with asyncio.timeout(remaining):
                        await receive_text(websocket, service.policy.max_message_bytes)
                    raise Rejected("invalid_message")
                except TimeoutError:
                    pass
            await close_socket(websocket, service.policy.send_timeout, 1000)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Rejected as exc:
            if accepted:
                await send_error(websocket, service, exc.code, request_id, exc.close_code)
        except AuthError as exc:
            code: ErrorCode = "rate_limited" if exc.code == "rate_limited" else "invalid_access"
            if accepted:
                await send_error(websocket, service, code, request_id, 1008)
        except RoomError as exc:
            code = ROOM_ERRORS.get(exc.code, "internal_error")
            if code == "dependency_unavailable":
                log_event(
                    logger,
                    "dependency_unavailable",
                    code="schema_mismatch",
                    dependency="redis",
                    operation="join",
                )
            elif code == "internal_error":
                log_event(logger, "unexpected_error", code="internal_error", operation="join")
            if accepted:
                close_code = 1013 if code == "dependency_unavailable" else 1008
                if code == "internal_error":
                    close_code = 1011
                await send_error(websocket, service, code, request_id, close_code)
        except (RedisError, DBAPIError, TimeoutError) as exc:
            dependency = "redis"
            if isinstance(exc, DBAPIError):
                dependency = "postgres"
            elif isinstance(exc, TimeoutError):
                dependency = operation_dependency
            log_event(
                logger,
                "dependency_unavailable",
                dependency=dependency,
                code="timeout" if isinstance(exc, TimeoutError) else f"{dependency}_unavailable",
                operation="join",
            )
            if accepted:
                await send_error(websocket, service, "dependency_unavailable", request_id, 1013)
        except OSError:
            pass
        except Exception:  # noqa: BLE001 -- WS boundary; never emit raw input or exception text.
            log_event(logger, "unexpected_error", code="internal_error", operation="join")
            if accepted:
                await send_error(websocket, service, "internal_error", request_id, 1011)
        finally:
            try:
                if accepted and not cancelled:
                    await close_socket(websocket, service.policy.send_timeout, 1000)
                await cleanup(service, attempt)
            finally:
                service.release()

    return router

```


## zvonilka/backend/src/zvonilka/signaling/schemas.py

```python
"""Initial signaling v1 contract. Validation is not authentication or admission."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from zvonilka.rooms.schemas import RoomView

PROTOCOL_VERSION = 1
MAX_ACCESS_TOKEN_LENGTH = 4096
MAX_CAPABILITIES = 16

Capability = Annotated[
    str, StringConstraints(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_.-]*$")
]
ApplicationVersion = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$")
]
ErrorCode = Literal[
    "invalid_message",
    "unsupported_version",
    "authentication_required",
    "authentication_timeout",
    "invalid_access",
    "room_not_found",
    "room_closed",
    "not_admitted",
    "room_limit",
    "owner_room_limit",
    "participant_limit",
    "overloaded",
    "registry_full",
    "stale_generation",
    "rate_limited",
    "message_too_large",
    "dependency_unavailable",
    "internal_error",
]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        return value


class Authenticate(Message):
    type: Literal["authenticate"]
    request_id: UUID
    room_id: UUID
    access_token: SecretStr = Field(min_length=1, max_length=MAX_ACCESS_TOKEN_LENGTH)
    app_version: ApplicationVersion
    capabilities: list[Capability] = Field(max_length=MAX_CAPABILITIES)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate capability")
        return value


class Joined(Message):
    type: Literal["joined"]
    request_id: UUID
    user_id: UUID
    generation: UUID
    room: RoomView
    capabilities: list[Capability] = Field(max_length=MAX_CAPABILITIES)

    @model_validator(mode="after")
    def online_member(self) -> "Joined":
        if self.room.state != "active":
            raise ValueError("joined requires an active room")
        ids = [member.user_id for member in self.room.members]
        if len(ids) != len(set(ids)) or not 1 <= len(ids) <= 3:
            raise ValueError("invalid room members")
        if not any(
            member.user_id == self.user_id and member.online for member in self.room.members
        ):
            raise ValueError("joined user must be online in the snapshot")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("duplicate capability")
        return self


class Error(Message):
    type: Literal["error"]
    request_id: UUID | None
    code: ErrorCode
    fatal: bool = Field(strict=True)


client_message_adapter = TypeAdapter(Authenticate)
server_message_adapter = TypeAdapter(Annotated[Joined | Error, Field(discriminator="type")])

```


## zvonilka/backend/src/zvonilka/signaling/service.py

```python
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

```


## zvonilka/backend/tests/signaling_test_support.py

```python
"""In-memory dependency doubles, not Redis/PostgreSQL or atomicity simulations."""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from zvonilka.auth.errors import AuthError


@dataclass(frozen=True)
class TestIdentity:
    user_id: UUID


class StubAuth:
    def __init__(self):
        self.user_id = uuid4()
        self.calls = 0
        self.error: Exception | None = None

    async def authenticate(self, access_token: str) -> TestIdentity:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if access_token != "test-access-token":
            raise AuthError("invalid_access")
        return TestIdentity(self.user_id)


class StubRooms:
    def __init__(self):
        self.joins: list[tuple[str, str, str]] = []
        self.leaves: list[tuple[str, str, str]] = []
        self.error: Exception | None = None
        self.leave_error: Exception | None = None
        self.join_started = asyncio.Event()
        self.join_gate: asyncio.Event | None = None
        self.bad_snapshot = False

    async def join(self, actor: str, room_id: str, generation: str) -> dict:
        self.joins.append((actor, room_id, generation))
        self.join_started.set()
        if self.join_gate is not None:
            await self.join_gate.wait()
        if self.error is not None:
            raise self.error
        return {
            "room": {
                "room_id": str(uuid4()) if self.bad_snapshot else room_id,
                "owner_id": actor,
                "state": "active",
                "revision": 2,
                "members": [{"user_id": actor, "online": True}],
                "activated_at": 1800000000,
                "deadline": 1800043200,
            }
        }

    async def leave(self, actor: str, room_id: str, generation: str) -> dict:
        self.leaves.append((actor, room_id, generation))
        if self.leave_error is not None:
            raise self.leave_error
        return {}


def authentication(room_id: UUID) -> dict:
    return {
        "version": 1,
        "type": "authenticate",
        "request_id": str(uuid4()),
        "room_id": str(room_id),
        "access_token": "test-access-token",
        "app_version": "0.5.0-dev.1",
        "capabilities": [],
    }

```


## zvonilka/backend/tests/test_signaling_integration.py

```python
"""Actual auth/Redis admission through the full app's ASGI WS interface."""

import os
import unittest
from uuid import uuid4

from auth_test_support import PG_URL, AuthPgFixture
from test_signaling_transport import WsSession

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


@unittest.skipUnless(PG_URL and REDIS_URL, "real PostgreSQL and Redis URLs are required")
class SignalingIntegrationTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from pydantic import SecretStr
        from zvonilka.api import create_app

        assert REDIS_URL is not None
        self.settings.redis_url = SecretStr(REDIS_URL)
        self.settings.room_registry_key = "zvonilka:ws-test:" + uuid4().hex
        self.asgi = create_app(self.settings)
        self.app = self.asgi.app
        context = self.app.router.lifespan_context(self.app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.addAsyncCleanup(self.cleanup_registry)
        self.app.state.auth.passwords = self.passwords
        profile = await self.app.state.auth.register("alice", "Alice", self.password)
        self.owner_id = str(profile["id"])
        pair = await self.app.state.auth.login("alice", self.password)
        self.token = pair["access_token"]
        self.room = await self.app.state.room_repository.create(self.owner_id)
        self.room_id = self.room["room_id"]
        self.path = f"/api/v1/rooms/{self.room_id}/ws"

    async def cleanup_registry(self):
        await self.app.state.redis.delete(self.settings.room_registry_key)

    def payload(self) -> dict:
        return {
            "version": 1,
            "type": "authenticate",
            "request_id": str(uuid4()),
            "room_id": self.room_id,
            "access_token": self.token,
            "app_version": "0.5.0-dev.1",
            "capabilities": [],
        }

    async def test_join_activates_and_disconnect_leaves_real_presence(self):
        repo = self.app.state.room_repository
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            joined = await ws.receive()
            self.assertEqual(joined["type"], "joined")
            snapshot = await repo.snapshot(self.owner_id, self.room_id)
            self.assertEqual(snapshot["state"], "active")
            self.assertEqual(snapshot["members"], [{"user_id": self.owner_id, "online": True}])
        snapshot = await repo.snapshot(self.owner_id, self.room_id)
        self.assertEqual(snapshot["state"], "recovering")
        self.assertFalse(snapshot["members"][0]["online"])
        self.assertEqual(self.app.state.signaling.connections, 0)

    async def test_invalid_token_does_not_activate_room(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send({**self.payload(), "access_token": "wrong"})
            self.assertEqual((await ws.receive())["code"], "invalid_access")
        snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
        self.assertEqual(snapshot["state"], "created")
        self.assertFalse(snapshot["members"][0]["online"])

    async def test_foreign_account_cannot_join(self):
        await self.app.state.auth.register("bobby", "Bob", self.password)
        pair = await self.app.state.auth.login("bobby", self.password)
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send({**self.payload(), "access_token": pair["access_token"]})
            self.assertEqual((await ws.receive())["code"], "not_admitted")
        snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
        self.assertEqual(snapshot["state"], "created")

    async def test_schema_mismatch_is_safe_and_does_not_mutate_redis(self):
        redis = self.app.state.redis
        raw = '{"schema":999,"rooms":{}}'
        await redis.set(self.settings.room_registry_key, raw)
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            response = await ws.receive()
            self.assertEqual(response["code"], "dependency_unavailable")
            self.assertNotIn(self.token, str(response))
        self.assertEqual(await redis.get(self.settings.room_registry_key), raw)


if __name__ == "__main__":
    unittest.main()

```


## zvonilka/backend/tests/test_signaling_network.py

```python
"""Real loopback TCP/HTTP Upgrade/WS frames; auth and rooms are dependency doubles."""

import asyncio
import json
import socket
import unittest
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from signaling_test_support import StubAuth, StubRooms, authentication
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import SignalingService


class SignalingNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(self.auth, self.rooms, presence_ttl=45)
        app = FastAPI()
        app.state.signaling = self.service
        app.include_router(signaling_router())
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.sock.close)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.setblocking(False)
        config = uvicorn.Config(
            app,
            lifespan="off",
            ws="websockets",
            log_config=None,
            access_log=False,
            ws_max_size=16384,
            ws_max_queue=4,
            ws_per_message_deflate=False,
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        self.addAsyncCleanup(self.stop_server)
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.server_task.done():
                    await self.server_task
                    self.fail("server exited before startup")
                await asyncio.sleep(0.01)
        self.room_id = uuid4()
        port = self.sock.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/api/v1/rooms/{self.room_id}/ws"

    async def stop_server(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=6)

    async def wait_for_cleanup(self):
        async with asyncio.timeout(5):
            while self.service.connections:
                await asyncio.sleep(0.01)

    async def test_upgrade_join_and_disconnect_over_tcp(self):
        async with asyncio.timeout(5):
            async with connect(self.url) as ws:
                await ws.send(json.dumps(authentication(self.room_id)))
                response = json.loads(await ws.recv())
                self.assertEqual(response["type"], "joined")
                self.assertEqual(response["user_id"], str(self.auth.user_id))
        await self.wait_for_cleanup()
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(len(self.rooms.joins), 1)

    async def test_backend_rejects_frame_above_limit(self):
        async with asyncio.timeout(5):
            async with connect(self.url) as ws:
                await ws.send("x" * 16385)
                with self.assertRaises(ConnectionClosed) as caught:
                    await ws.recv()
                self.assertIsNotNone(caught.exception.rcvd)
                if caught.exception.rcvd is not None:
                    self.assertEqual(caught.exception.rcvd.code, 1009)
        await self.wait_for_cleanup()
        self.assertEqual(self.auth.calls, 0)
        self.assertEqual(self.rooms.joins, [])


if __name__ == "__main__":
    unittest.main()

```


## zvonilka/backend/tests/test_signaling_schemas.py

```python
import json
import unittest
from uuid import uuid4

from pydantic import ValidationError
from zvonilka.signaling.schemas import (
    Authenticate,
    Error,
    Joined,
    client_message_adapter,
    server_message_adapter,
)


class SignalingSchemaTests(unittest.TestCase):
    def setUp(self):
        self.user_id = str(uuid4())
        self.room_id = str(uuid4())
        self.request_id = str(uuid4())
        self.token = "unit-test-access-token-DO-NOT-LOG"

    def authentication(self) -> dict:
        return {
            "version": 1,
            "type": "authenticate",
            "request_id": self.request_id,
            "room_id": self.room_id,
            "access_token": self.token,
            "app_version": "0.5.0-dev.1+local",
            "capabilities": [],
        }

    def joined(self) -> dict:
        return {
            "version": 1,
            "type": "joined",
            "request_id": self.request_id,
            "user_id": self.user_id,
            "generation": str(uuid4()),
            "room": {
                "room_id": self.room_id,
                "owner_id": self.user_id,
                "state": "active",
                "revision": 2,
                "members": [{"user_id": self.user_id, "online": True}],
                "activated_at": "2026-11-15T12:00:00Z",
                "deadline": "2026-11-16T00:00:00Z",
            },
            "capabilities": [],
        }

    def error(self) -> dict:
        return {
            "version": 1,
            "type": "error",
            "request_id": None,
            "code": "invalid_message",
            "fatal": True,
        }

    def test_authentication_from_json(self):
        item = client_message_adapter.validate_json(json.dumps(self.authentication()))
        self.assertIsInstance(item, Authenticate)
        self.assertEqual(str(item.room_id), self.room_id)
        self.assertEqual(item.access_token.get_secret_value(), self.token)

    def test_authentication_round_trip_requires_explicit_secret_access(self):
        item = Authenticate.model_validate(self.authentication())
        data = item.model_dump(mode="json")
        self.assertNotIn(self.token, json.dumps(data))
        data["access_token"] = item.access_token.get_secret_value()
        self.assertEqual(client_message_adapter.validate_json(json.dumps(data)), item)

    def test_token_redacted_in_default_representations(self):
        item = Authenticate.model_validate(self.authentication())
        for value in (repr(item), str(item), repr(item.model_dump()), item.model_dump_json()):
            with self.subTest(value_type=type(value).__name__):
                self.assertNotIn(self.token, value)

    def test_server_json_round_trip(self):
        for payload, expected in ((self.joined(), Joined), (self.error(), Error)):
            with self.subTest(message_type=payload["type"]):
                item = server_message_adapter.validate_json(json.dumps(payload))
                self.assertIsInstance(item, expected)
                self.assertEqual(server_message_adapter.validate_json(item.model_dump_json()), item)

    def test_version_is_required_and_exact_integer(self):
        for factory, model in (
            (self.authentication, Authenticate),
            (self.joined, Joined),
            (self.error, Error),
        ):
            for version in (0, 2, True, False, 1.0, "1", None):
                with self.subTest(model=model.__name__, version=version):
                    data = factory()
                    data["version"] = version
                    with self.assertRaises(ValidationError):
                        model.model_validate(data)
            data = factory()
            del data["version"]
            with self.assertRaises(ValidationError):
                model.model_validate(data)

    def test_authentication_required_fields(self):
        original = self.authentication()
        for name in original:
            with self.subTest(field=name):
                data = dict(original)
                del data[name]
                with self.assertRaises(ValidationError):
                    Authenticate.model_validate(data)

    def test_extra_identity_and_generation_rejected(self):
        for name in ("user_id", "session_id", "generation", "unexpected"):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), name: str(uuid4())})

    def test_extra_server_fields_rejected(self):
        for factory, model in ((self.joined, Joined), (self.error, Error)):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate({**factory(), "detail": self.token})

    def test_bad_uuids_rejected(self):
        for factory, model, names in (
            (self.authentication, Authenticate, ("request_id", "room_id")),
            (self.joined, Joined, ("request_id", "user_id", "generation")),
            (self.error, Error, ("request_id",)),
        ):
            for name in names:
                with (
                    self.subTest(model=model.__name__, field=name),
                    self.assertRaises(ValidationError),
                ):
                    model.model_validate({**factory(), name: "not-a-uuid"})

    def test_token_boundaries_and_types(self):
        for length in (1, 4096):
            item = Authenticate.model_validate(
                {**self.authentication(), "access_token": "a" * length}
            )
            self.assertEqual(len(item.access_token.get_secret_value()), length)
        for value in ("", "a" * 4097, None, 123, True, []):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), "access_token": value})

    def test_application_version_boundaries(self):
        for value in ("a", "a" * 64):
            Authenticate.model_validate({**self.authentication(), "app_version": value})
        for value in ("", "a" * 65, " 1.0", "1.0\n", "версия", 1, None):
            with self.subTest(version=value), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), "app_version": value})

    def test_capabilities_boundaries_and_unknown_names(self):
        for values in ([], ["future.audio-v2"], ["a" * 48], [f"cap{i}" for i in range(16)]):
            item = Authenticate.model_validate({**self.authentication(), "capabilities": values})
            self.assertEqual(item.capabilities, values)
        for values in (
            ["a", "a"],
            [""],
            ["a" * 49],
            ["UPPER"],
            ["a\n"],
            [f"cap{i}" for i in range(17)],
            [1],
            None,
        ):
            with self.subTest(capabilities=values), self.assertRaises(ValidationError):
                Authenticate.model_validate({**self.authentication(), "capabilities": values})

    def test_unknown_type_and_direction_rejected(self):
        for adapter, payload in (
            (client_message_adapter, {**self.authentication(), "type": "offer"}),
            (client_message_adapter, self.joined()),
            (server_message_adapter, {**self.error(), "type": "unknown"}),
            (server_message_adapter, self.authentication()),
        ):
            with self.subTest(message_type=payload["type"]), self.assertRaises(ValidationError):
                adapter.validate_python(payload)

    def test_non_object_and_invalid_json_rejected(self):
        for adapter in (client_message_adapter, server_message_adapter):
            for raw in ("{", "[]", "null", '"text"', "123"):
                with self.subTest(raw=raw), self.assertRaises(ValidationError):
                    adapter.validate_json(raw)

    def test_joined_requires_online_user_and_active_room(self):
        for state in ("created", "recovering", "closed"):
            data = self.joined()
            data["room"]["state"] = state
            with self.subTest(state=state), self.assertRaises(ValidationError):
                Joined.model_validate(data)
        for members in (
            [],
            [{"user_id": self.user_id, "online": False}],
            [{"user_id": str(uuid4()), "online": True}],
        ):
            data = self.joined()
            data["room"]["members"] = members
            with self.subTest(members=members), self.assertRaises(ValidationError):
                Joined.model_validate(data)

    def test_joined_members_bounds_and_duplicates(self):
        data = self.joined()
        data["room"]["members"] *= 2
        with self.assertRaises(ValidationError):
            Joined.model_validate(data)
        data = self.joined()
        data["room"]["members"] += [{"user_id": str(uuid4()), "online": True} for _ in range(2)]
        Joined.model_validate(data)
        data["room"]["members"].append({"user_id": str(uuid4()), "online": True})
        with self.assertRaises(ValidationError):
            Joined.model_validate(data)

    def test_joined_rejects_duplicate_capabilities(self):
        with self.assertRaises(ValidationError):
            Joined.model_validate({**self.joined(), "capabilities": ["a", "a"]})

    def test_error_codes_and_fatal_flag_are_bounded(self):
        for code in ("arbitrary_exception_text", "schema_mismatch", self.token):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                Error.model_validate({**self.error(), "code": code})
        for value in (0, 1, "true", None):
            with self.subTest(fatal=value), self.assertRaises(ValidationError):
                Error.model_validate({**self.error(), "fatal": value})
        for value in (True, False):
            self.assertEqual(Error.model_validate({**self.error(), "fatal": value}).fatal, value)
        item = Error.model_validate({**self.error(), "request_id": self.request_id})
        self.assertEqual(str(item.request_id), self.request_id)


if __name__ == "__main__":
    unittest.main()

```


## zvonilka/backend/tests/test_signaling_service.py

```python
import asyncio
import json
import unittest
from time import monotonic
from uuid import UUID, uuid4

from signaling_test_support import StubAuth, StubRooms, authentication
from zvonilka.auth.errors import AuthError
from zvonilka.rooms.repository import RoomError
from zvonilka.signaling.service import Policy, Rejected, SignalingService, parse_authentication


class SignalingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(self.auth, self.rooms, presence_ttl=45)
        self.room_id = uuid4()
        self.message = parse_authentication(json.dumps(authentication(self.room_id)))

    async def test_join_uses_wrapped_lua_snapshot_and_verified_identity(self):
        attempt = await self.service.identify(self.message)
        response = await self.service.join(attempt, self.message.request_id)
        self.assertEqual(response.user_id, self.auth.user_id)
        self.assertEqual(response.room.room_id, self.room_id)
        self.assertEqual(response.request_id, self.message.request_id)
        self.assertEqual(response.generation, attempt.generation)
        self.assertEqual(response.capabilities, [])
        self.assertEqual(UUID(self.rooms.joins[0][2]), attempt.generation)
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_bad_auth_does_not_join(self):
        self.auth.error = AuthError("invalid_access")
        with self.assertRaises(AuthError):
            await self.service.identify(self.message)
        self.assertEqual(self.rooms.joins, [])

    async def test_no_leave_before_join_attempt(self):
        attempt = await self.service.identify(self.message)
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, [])

    async def test_error_after_join_attempt_still_allows_generation_cleanup(self):
        attempt = await self.service.identify(self.message)
        self.rooms.error = RoomError("not_admitted")
        with self.assertRaises(RoomError):
            await self.service.join(attempt, self.message.request_id)
        self.assertTrue(attempt.join_attempted)
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_cancelled_join_keeps_cleanup_identity(self):
        self.rooms.join_gate = asyncio.Event()
        attempt = await self.service.identify(self.message)
        task = asyncio.create_task(self.service.join(attempt, self.message.request_id))
        await self.rooms.join_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_different_snapshot_room_is_not_success(self):
        self.rooms.bad_snapshot = True
        attempt = await self.service.identify(self.message)
        with self.assertRaises(RuntimeError):
            await self.service.join(attempt, self.message.request_id)

    async def test_generation_is_fresh_for_each_attempt(self):
        first = await self.service.identify(self.message)
        second = await self.service.identify(self.message)
        self.assertNotEqual(first.generation, second.generation)

    async def test_short_session_does_not_outlive_lease_window(self):
        service = SignalingService(self.auth, self.rooms, presence_ttl=2)
        before = monotonic()
        attempt = await service.identify(self.message)
        self.assertEqual(service.session_seconds, 1)
        self.assertGreater(attempt.deadline, before)
        self.assertLessEqual(attempt.deadline - monotonic(), 1)

    async def test_connections_have_a_hard_local_cap(self):
        service = SignalingService(
            self.auth, self.rooms, presence_ttl=45, policy=Policy(max_connections=1)
        )
        self.assertTrue(service.acquire())
        self.assertFalse(service.acquire())
        service.release()
        self.assertTrue(service.acquire())
        service.release()
        self.assertEqual(service.connections, 0)

    async def test_parser_rejects_untrusted_identity(self):
        payload = {**authentication(self.room_id), "user_id": str(uuid4())}
        with self.assertRaises(Rejected) as caught:
            parse_authentication(json.dumps(payload))
        self.assertEqual(caught.exception.code, "invalid_message")

    async def test_parser_classifies_unsupported_version(self):
        for version, code in (
            (2, "unsupported_version"),
            (True, "invalid_message"),
            ("1", "invalid_message"),
            (None, "invalid_message"),
        ):
            with self.subTest(version=version), self.assertRaises(Rejected) as caught:
                parse_authentication(
                    json.dumps({**authentication(self.room_id), "version": version})
                )
            self.assertEqual(caught.exception.code, code)

    async def test_parser_rejects_invalid_json_and_shape(self):
        for raw in ("{", "null", "[]", '"secret"', '{"version": 1}'):
            with self.subTest(raw=raw), self.assertRaises(Rejected) as caught:
                parse_authentication(raw)
            self.assertEqual(caught.exception.code, "invalid_message")

    async def test_policy_rejects_invalid_resource_limits(self):
        with self.assertRaises(ValueError):
            Policy(auth_timeout=0)
        with self.assertRaises(ValueError):
            Policy(session_seconds=float("inf"))
        with self.assertRaises(ValueError):
            Policy(max_connections=True)
        with self.assertRaises(ValueError):
            Policy(max_message_bytes=0)


if __name__ == "__main__":
    unittest.main()

```


## zvonilka/backend/tests/test_signaling_transport.py

```python
"""ASGI WS transport tests with dependency doubles; not a TCP/Redis test."""

import asyncio
import json
import unittest
from dataclasses import replace
from typing import Self
from uuid import uuid4

from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from signaling_test_support import StubAuth, StubRooms, authentication
from starlette.types import ASGIApp, Message, Scope
from zvonilka.http_boundary import SafeHttpBoundary
from zvonilka.rooms.repository import RoomError
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import Policy, SignalingService


class WsSession:
    def __init__(self, app: ASGIApp, path: str, query: bytes = b""):
        self.app = app
        self.scope: Scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query,
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "subprotocols": [],
            "state": {},
        }
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.outgoing: asyncio.Queue[Message] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def run_app(self) -> None:
        await self.app(self.scope, self.incoming.get, self.outgoing.put)

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self.run_app())
        await self.incoming.put({"type": "websocket.connect"})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self.task is not None
        if not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, timeout=6)

    async def event(self) -> Message:
        try:
            return await asyncio.wait_for(self.outgoing.get(), timeout=2)
        except TimeoutError:
            if self.task is not None and self.task.done():
                await self.task
            raise

    async def accepted(self) -> None:
        event = await self.event()
        if event["type"] != "websocket.accept":
            raise AssertionError(f"expected accept, got {event['type']}")

    async def send(self, payload: dict) -> None:
        await self.raw(json.dumps(payload))

    async def raw(self, raw: str) -> None:
        await self.incoming.put({"type": "websocket.receive", "text": raw})

    async def receive(self) -> dict:
        event = await self.event()
        if event["type"] != "websocket.send":
            raise AssertionError(f"expected message, got {event['type']}")
        return json.loads(event["text"])


class SignalingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.policy = Policy(
            auth_timeout=0.1,
            operation_timeout=0.2,
            send_timeout=0.1,
            cleanup_timeout=0.1,
            session_seconds=1,
            max_connections=2,
        )
        self.service = SignalingService(self.auth, self.rooms, presence_ttl=45, policy=self.policy)
        self.app = FastAPI()
        self.app.state.signaling = self.service
        self.app.include_router(signaling_router())
        self.asgi = SafeHttpBoundary(self.app)
        self.room_id = uuid4()
        self.path = f"/api/v1/rooms/{self.room_id}/ws"

    async def assert_error(self, ws: WsSession, code: str) -> dict:
        message = await ws.receive()
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["code"], code)
        self.assertTrue(message["fatal"])
        self.assertNotIn("test-access-token", json.dumps(message))
        close = await ws.event()
        self.assertEqual(close["type"], "websocket.close")
        return message

    async def test_join_then_disconnect_cleans_generation_and_slot(self):
        payload = authentication(self.room_id)
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(payload)
            joined = await ws.receive()
            self.assertEqual(joined["type"], "joined")
            self.assertEqual(joined["request_id"], payload["request_id"])
            self.assertEqual(joined["user_id"], str(self.auth.user_id))
            self.assertEqual(joined["generation"], self.rooms.joins[0][2])
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(self.service.connections, 0)

    async def test_invalid_token_never_joins(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send({**authentication(self.room_id), "access_token": "wrong"})
            await self.assert_error(ws, "invalid_access")
        self.assertEqual(self.rooms.joins, [])
        self.assertEqual(self.rooms.leaves, [])

    async def test_other_room_is_rejected_by_repository(self):
        self.rooms.error = RoomError("not_admitted")
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            await self.assert_error(ws, "not_admitted")

    async def test_url_and_message_room_must_match(self):
        payload = authentication(uuid4())
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(payload)
            message = await self.assert_error(ws, "invalid_message")
            self.assertEqual(message["request_id"], payload["request_id"])
        self.assertEqual(self.auth.calls, 0)

    async def test_malformed_first_message_is_safe(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.raw('{"access_token": "test-access-token",')
            message = await self.assert_error(ws, "invalid_message")
            self.assertIsNone(message["request_id"])
        self.assertEqual(self.auth.calls, 0)

    async def test_authentication_wait_has_a_deadline(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await self.assert_error(ws, "authentication_timeout")
        self.assertEqual(self.service.connections, 0)

    async def test_oversized_message_never_reaches_auth(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.raw("я" * 8193)
            await self.assert_error(ws, "message_too_large")
        self.assertEqual(self.auth.calls, 0)

    async def test_binary_first_message_rejected(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.incoming.put({"type": "websocket.receive", "bytes": b"data"})
            await self.assert_error(ws, "invalid_message")

    async def test_redis_failure_never_returns_joined_and_attempts_cleanup(self):
        self.rooms.error = RedisConnectionError("test-access-token")
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            await self.assert_error(ws, "dependency_unavailable")
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(self.service.connections, 0)

    async def test_join_timeout_attempts_cleanup(self):
        self.rooms.join_gate = asyncio.Event()
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            await self.assert_error(ws, "dependency_unavailable")
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_handler_cancellation_cleans_attempt_and_releases_slot(self):
        self.rooms.join_gate = asyncio.Event()
        with self.assertRaises(asyncio.CancelledError):
            async with WsSession(self.asgi, self.path) as ws:
                await ws.accepted()
                await ws.send(authentication(self.room_id))
                await asyncio.wait_for(self.rooms.join_started.wait(), timeout=2)
                assert ws.task is not None
                ws.task.cancel()
                await ws.task
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(self.service.connections, 0)

    async def test_stale_cleanup_does_not_fail_disconnect(self):
        self.rooms.leave_error = RoomError("stale_generation")
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            self.assertEqual((await ws.receive())["type"], "joined")
        self.assertEqual(self.service.connections, 0)

    async def test_cleanup_redis_failure_releases_slot(self):
        self.rooms.leave_error = RedisConnectionError("test-access-token")
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            self.assertEqual((await ws.receive())["type"], "joined")
        self.assertEqual(self.service.connections, 0)

    async def test_query_is_rejected_before_accept(self):
        async with WsSession(self.asgi, self.path, b"token=secret") as ws:
            self.assertEqual((await ws.event())["type"], "websocket.close")
        self.assertEqual(self.auth.calls, 0)

    async def test_slot_limit_rejects_without_accept(self):
        self.service.policy = replace(self.policy, max_connections=1)
        async with WsSession(self.asgi, self.path) as first:
            await first.accepted()
            async with WsSession(self.asgi, self.path) as second:
                self.assertEqual((await second.event())["type"], "websocket.close")
        self.assertEqual(self.service.connections, 0)

    async def test_short_session_closes_and_leaves(self):
        self.service.session_seconds = 0.05
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            self.assertEqual((await ws.receive())["type"], "joined")
            close = await ws.event()
            self.assertEqual(close["type"], "websocket.close")
            self.assertEqual(close["code"], 1000)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_no_offer_forwarding_in_this_delivery(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            self.assertEqual((await ws.receive())["type"], "joined")
            await ws.send({"version": 1, "type": "offer"})
            await self.assert_error(ws, "invalid_message")


if __name__ == "__main__":
    unittest.main()

```


---

# Отредактированные секреты

Ничего не найдено/заменено.
