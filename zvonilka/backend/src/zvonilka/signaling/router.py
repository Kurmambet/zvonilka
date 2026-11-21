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
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from zvonilka.auth.errors import AuthError
from zvonilka.observability.logging import log_event
from zvonilka.rooms.repository import RoomError

from .auth_protocol import Reauthenticate, ReauthRequired
from .schemas import Error, ErrorCode, Heartbeat
from .sender import SocketWriter
from .service import (
    Attempt,
    Connection,
    Rejected,
    SignalingService,
    parse_active,
    parse_authentication,
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
        ready=partial(service.can_receive_relay, attempt),
        display_name=attempt.display_name,
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


async def watch_session(service: SignalingService, attempt: Attempt) -> None:
    # Independent of heartbeat and refresh; never retain the access token here.
    window = attempt.authorization
    connection = attempt.connection
    try:
        while not connection.stopped.is_set():
            await asyncio.sleep(service.policy.session_check_interval)
            if connection.stopped.is_set():
                return
            async with asyncio.timeout(service.policy.operation_timeout):
                await service.auth.check_session(window.user_id, window.session_id)
    except AuthError:
        connection.stop("session_revoked")
    except (SQLAlchemyError, TimeoutError, OSError):
        connection.stop("dependency_unavailable")
    except Exception:  # noqa: BLE001 -- Fail closed; never expose DB exception details.
        connection.stop("internal_error")


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
        session_watch: asyncio.Task[None] | None = None
        operation_dependency = "postgres"
        operation = "join"

        started_at = monotonic()
        auth_request_id: str | None = None
        generation: str | None = None
        joined = False
        ws_code = "handler_interrupted"
        observed_close_code: int | None = None
        requested_close_code: int | None = None

        def reject_before_join(code: ErrorCode) -> None:
            if not joined and auth_request_id is not None:
                log_event(
                    logger,
                    "ws_auth_rejected",
                    ws_code=code,
                    auth_request_id=auth_request_id,
                    generation=generation,
                )

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
            auth_request_id = str(request_id)
            if message.room_id != target:
                raise Rejected("invalid_message")
            del raw
            async with asyncio.timeout(service.policy.operation_timeout):
                attempt = await interrupted(connection, partial(service.identify, message))
                generation = str(attempt.generation)
                del message
                attempt.connection = connection
                operation_dependency = "redis"
                # Bind stable locals for the callback and type checker.
                current = attempt
                join_request_id = request_id
                await interrupted(
                    connection, partial(join_and_publish, service, current, join_request_id, writer)
                )

            joined = True
            log_event(
                logger,
                "ws_joined",
                auth_request_id=auth_request_id,
                generation=generation,
            )
            session_watch = asyncio.create_task(watch_session(service, current))
            operation = "heartbeat"
            notified = False
            while True:
                request_id = None
                now = monotonic()
                state = attempt.authorization.state(now)
                if state == "expired":
                    raise Rejected("reauthentication_timeout")
                if attempt.deadline <= now:
                    raise Rejected("heartbeat_timeout")
                if state == "reauth_required" and not notified:
                    notice = ReauthRequired(
                        version=1, type="reauth_required", generation=attempt.generation
                    )
                    writer.send(notice.model_dump_json())
                    notified = True
                # One receive task at a time; no separate watchdog until 4.5b4.
                wait = min(attempt.deadline, attempt.authorization.next_transition(now)) - now
                try:
                    async with asyncio.timeout(wait):
                        raw = await interrupted(
                            connection,
                            partial(receive_text, websocket, service.policy.max_message_bytes),
                        )
                except TimeoutError:
                    continue
                active = parse_active(raw)
                del raw
                request_id = active.request_id
                if attempt.authorization.state(monotonic()) == "expired":
                    raise Rejected("reauthentication_timeout")
                timeout = min(
                    service.policy.operation_timeout,
                    max(0.0, attempt.authorization.grace_deadline - monotonic()),
                )
                try:
                    if isinstance(active, Heartbeat):
                        operation = "heartbeat"
                        operation_dependency = "redis"
                        async with asyncio.timeout(timeout):
                            ack = await interrupted(
                                connection, partial(service.heartbeat, current, active)
                            )
                        writer.send(ack.model_dump_json())
                    elif isinstance(active, Reauthenticate):
                        operation = "reauthenticate"
                        operation_dependency = "postgres"
                        async with asyncio.timeout(timeout):
                            renewed = await interrupted(
                                connection, partial(service.reauthenticate, current, active)
                            )
                        writer.send(renewed.model_dump_json())
                        notified = False
                    else:
                        operation = "relay"
                        error = service.relay(current, active)
                        if error is not None:
                            writer.send(error.model_dump_json())
                except TimeoutError:
                    if attempt.authorization.state(monotonic()) == "expired":
                        raise Rejected("reauthentication_timeout") from None
                    raise

        except WebSocketDisconnect as exc:
            ws_code = connection.reason or "peer_disconnected"
            observed_close_code = exc.code
        except Rejected as exc:
            ws_code = exc.code
            reject_before_join(exc.code)
            if writer is not None:
                error_request_id = request_id
                if connection.reason == exc.code:
                    error_request_id = None
                send_error(writer, exc.code, error_request_id, exc.close_code)
                requested_close_code = exc.close_code
        except AuthError as exc:
            auth_code: ErrorCode = (
                "rate_limited" if exc.code == "rate_limited" else "invalid_access"
            )
            ws_code = auth_code
            reject_before_join(auth_code)
            if writer is not None:
                send_error(writer, auth_code, request_id, 1008)
                requested_close_code = 1008
        except RoomError as exc:
            room_code = ROOM_ERRORS.get(exc.code, "internal_error")
            ws_code = room_code
            reject_before_join(room_code)
            if room_code == "dependency_unavailable":
                log_event(
                    logger,
                    "dependency_unavailable",
                    code="schema_mismatch",
                    dependency="redis",
                    operation=operation,
                    auth_request_id=auth_request_id,
                    generation=generation,
                )
            elif room_code == "internal_error":
                log_event(
                    logger,
                    "unexpected_error",
                    code="internal_error",
                    operation=operation,
                    auth_request_id=auth_request_id,
                    generation=generation,
                )
            if writer is not None:
                close_code = 1013 if room_code == "dependency_unavailable" else 1008
                if room_code == "internal_error":
                    close_code = 1011
                send_error(writer, room_code, request_id, close_code)
                requested_close_code = close_code
        except (RedisError, DBAPIError, TimeoutError) as exc:
            ws_code = "dependency_unavailable"
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
                auth_request_id=auth_request_id,
                generation=generation,
            )
            if writer is not None:
                send_error(writer, "dependency_unavailable", request_id, 1013)
                requested_close_code = 1013
        except OSError:
            ws_code = connection.reason or "transport_error"
        except asyncio.CancelledError:
            ws_code = connection.reason or "handler_cancelled"
            raise
        except Exception:  # noqa: BLE001 -- WS boundary; never emit raw input or exception text.
            ws_code = "internal_error"
            log_event(
                logger,
                "unexpected_error",
                code="internal_error",
                operation=operation,
                auth_request_id=auth_request_id,
                generation=generation,
            )
            if writer is not None:
                send_error(writer, "internal_error", request_id, 1011)
                requested_close_code = 1011
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    try:
                        if session_watch is not None:
                            session_watch.cancel()
                            await asyncio.gather(session_watch, return_exceptions=True)
                        if attempt is not None:
                            service.participants.detach(
                                attempt.room_id, attempt.user_id, attempt.generation
                            )
                    finally:
                        try:
                            if writer is not None:
                                await writer.aclose()
                            elif accepted:
                                requested_close_code = 1000
                                await close_socket(websocket, service.policy.send_timeout, 1000)
                        finally:
                            await cleanup(service, attempt)
            finally:
                service.release(connection)
                log_event(
                    logger,
                    "ws_finished",
                    ws_code=ws_code,
                    auth_request_id=auth_request_id,
                    generation=generation,
                    observed_close_code=observed_close_code,
                    requested_close_code=requested_close_code,
                    duration_ms=(monotonic() - started_at) * 1000,
                )

    return router
