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
            log_event(
                logging.getLogger("zvonilka.api"),
                "service_started",
                environment=settings.environment,
            )
            yield
        finally:
            try:
                if started:
                    await app.state.signaling.shutdown()
                    log_event(logging.getLogger("zvonilka.api"), "service_stopped")
            finally:
                try:
                    await redis.aclose()
                finally:
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
