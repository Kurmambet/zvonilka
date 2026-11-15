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
