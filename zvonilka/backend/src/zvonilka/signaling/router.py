"""Bounded WS admission, generation-aware heartbeat and cooperative shutdown."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
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
from .sender import SocketWriter
from .service import (
    Attempt,
    Connection,
    Rejected,
    SignalingService,
    parse_authentication,
    parse_heartbeat,
)

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


async def interrupted[T](connection: Connection, action: Callable[[], Awaitable[T]]) -> T:
    """Wake blocked IO on replacement/shutdown, and always reap both child tasks."""
    connection.check()

    async def run_action() -> T:
        return await action()

    work = asyncio.create_task(run_action())
    stopped = asyncio.create_task(connection.stopped.wait())
    try:
        await asyncio.wait((work, stopped), return_when=asyncio.FIRST_COMPLETED)
        connection.check()
        return await work
    finally:
        for task in (work, stopped):
            if not task.done():
                task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(work, stopped, return_exceptions=True)


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


def send_error(
    writer: SocketWriter,
    code: ErrorCode,
    request_id: UUID | None,
    close_code: int,
) -> None:
    response = Error(version=1, type="error", request_id=request_id, code=code, fatal=True)
    writer.request_close(close_code, final_message=response.model_dump_json())


async def join_and_publish(
    service: SignalingService, attempt: Attempt, request_id: UUID, writer: SocketWriter
) -> None:
    await service.participants.admit(
        attempt.room_id,
        attempt.user_id,
        attempt.generation,
        writer.send,
        attempt.connection.stop,
        partial(service.join, attempt, request_id),
    )


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
    finally:
        service.forget(attempt)


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
        connection = service.track()
        attempt: Attempt | None = None
        request_id: UUID | None = None
        accepted = False
        writer: SocketWriter | None = None
        operation_dependency = "postgres"
        operation = "join"
        try:
            async with asyncio.timeout(service.policy.send_timeout):
                await websocket.accept()
            accepted = True
            writer = SocketWriter(
                websocket,
                connection,
                max_message_bytes=service.policy.max_message_bytes,
                send_timeout=service.policy.send_timeout,
                close_timeout=service.policy.send_timeout,
            )
            writer.start()
            try:
                async with asyncio.timeout(service.policy.auth_timeout):
                    raw = await interrupted(
                        connection,
                        partial(receive_text, websocket, service.policy.max_message_bytes),
                    )
            except TimeoutError:
                raise Rejected("authentication_timeout") from None
            message = parse_authentication(raw)
            request_id = message.request_id
            if message.room_id != target:
                raise Rejected("invalid_message")
            del raw
            async with asyncio.timeout(service.policy.operation_timeout):
                attempt = await interrupted(connection, partial(service.identify, message))
                del message
                attempt.connection = connection
                operation_dependency = "redis"
                # Bind stable locals for the callback and type checker.
                current = attempt
                join_request_id = request_id
                await interrupted(
                    connection, partial(join_and_publish, service, current, join_request_id, writer)
                )
            operation = "heartbeat"
            while True:
                request_id = None
                remaining = attempt.deadline - monotonic()
                if remaining <= 0:
                    raise Rejected("heartbeat_timeout")
                try:
                    async with asyncio.timeout(remaining):
                        raw = await interrupted(
                            connection,
                            partial(receive_text, websocket, service.policy.max_message_bytes),
                        )
                except TimeoutError:
                    raise Rejected("heartbeat_timeout") from None
                heartbeat = parse_heartbeat(raw)
                del raw
                request_id = heartbeat.request_id
                async with asyncio.timeout(service.policy.operation_timeout):
                    ack = await interrupted(
                        connection, partial(service.heartbeat, current, heartbeat)
                    )
                writer.send(ack.model_dump_json())

        except WebSocketDisconnect:
            pass
        except Rejected as exc:
            if writer is not None:
                error_request_id = request_id
                if exc.code in {"connection_replaced", "server_shutdown"}:
                    error_request_id = None
                send_error(writer, exc.code, error_request_id, exc.close_code)
        except AuthError as exc:
            code: ErrorCode = "rate_limited" if exc.code == "rate_limited" else "invalid_access"
            if writer is not None:
                send_error(writer, code, request_id, 1008)
        except RoomError as exc:
            code = ROOM_ERRORS.get(exc.code, "internal_error")
            if code == "dependency_unavailable":
                log_event(
                    logger,
                    "dependency_unavailable",
                    code="schema_mismatch",
                    dependency="redis",
                    operation=operation,
                )
            elif code == "internal_error":
                log_event(logger, "unexpected_error", code="internal_error", operation=operation)
            if writer is not None:
                close_code = 1013 if code == "dependency_unavailable" else 1008
                if code == "internal_error":
                    close_code = 1011
                send_error(writer, code, request_id, close_code)
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
                operation=operation,
            )
            if writer is not None:
                send_error(writer, "dependency_unavailable", request_id, 1013)
        except OSError:
            pass
        except Exception:  # noqa: BLE001 -- WS boundary; never emit raw input or exception text.
            log_event(logger, "unexpected_error", code="internal_error", operation=operation)
            if writer is not None:
                send_error(writer, "internal_error", request_id, 1011)
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    try:
                        if attempt is not None:
                            service.participants.detach(
                                attempt.room_id, attempt.user_id, attempt.generation
                            )
                    finally:
                        try:
                            if writer is not None:
                                await writer.aclose()
                            elif accepted:
                                await close_socket(websocket, service.policy.send_timeout, 1000)
                        finally:
                            await cleanup(service, attempt)
            finally:
                service.release(connection)

    return router
