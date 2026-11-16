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
            heartbeat_timeout=1,
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

    async def test_missing_heartbeat_closes_and_leaves(self):
        self.service.heartbeat_window = 0.15
        self.service.heartbeat_interval = 0.05
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            self.assertEqual((await ws.receive())["type"], "joined")
            error = await self.assert_error(ws, "heartbeat_timeout")
            self.assertIsNone(error["request_id"])
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
