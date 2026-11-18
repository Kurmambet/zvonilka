"""ASGI lifecycle checks with explicit auth and Redis doubles."""

import asyncio
import unittest
from uuid import UUID, uuid4

from fastapi import FastAPI
from signaling_test_support import StubAuth, StubRooms, TestIdentity, authentication
from test_signaling_transport import WsSession
from zvonilka.auth.errors import AuthError
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import Policy, SignalingService


class WatchAuth(StubAuth):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.revoked = False

    async def check_session(self, user_id: UUID, session_id: UUID) -> TestIdentity:
        self.started.set()
        try:
            if self.revoked:
                raise AuthError("invalid_access")
            await asyncio.Event().wait()
        finally:
            self.finished.set()


class SessionWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = WatchAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(
            self.auth,
            self.rooms,
            presence_ttl=45,
            policy=Policy(session_check_interval=0.01, operation_timeout=0.5),
        )
        self.app = FastAPI()
        self.app.state.signaling = self.service
        self.app.include_router(signaling_router())
        self.room = uuid4()
        self.path = f"/api/v1/rooms/{self.room}/ws"

    async def join(self, ws: WsSession) -> None:
        await ws.accepted()
        await ws.send(authentication(self.room))
        self.assertEqual((await ws.receive_wire())["type"], "joined")
        self.assertEqual((await ws.receive_wire())["type"], "participants")

    async def assert_closed(self, ws: WsSession, code: str, close_code: int) -> None:
        error = await ws.receive_wire()
        self.assertEqual(error["code"], code)
        self.assertTrue(error["fatal"])
        self.assertIsNone(error["request_id"])
        event = await ws.event()
        self.assertEqual(event["type"], "websocket.close")
        self.assertEqual(event.get("code"), close_code)

    async def test_revocation_closes_idle_connection(self):
        self.auth.revoked = True
        async with WsSession(self.app, self.path) as ws:
            await self.join(ws)
            await self.assert_closed(ws, "session_revoked", 1008)
        self.assertTrue(self.auth.finished.is_set())
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_timeout_fails_closed(self):
        async with WsSession(self.app, self.path) as ws:
            await self.join(ws)
            await self.assert_closed(ws, "dependency_unavailable", 1013)
        self.assertTrue(self.auth.finished.is_set())

    async def test_disconnect_cancels_running_check(self):
        async with WsSession(self.app, self.path) as ws:
            await self.join(ws)
            await asyncio.wait_for(self.auth.started.wait(), 1)
        self.assertTrue(self.auth.finished.is_set())
        self.assertEqual(self.service.connections, 0)

    async def test_shutdown_cancels_running_check(self):
        async with WsSession(self.app, self.path) as ws:
            await self.join(ws)
            await asyncio.wait_for(self.auth.started.wait(), 1)
            await self.service.shutdown()
            await self.assert_closed(ws, "server_shutdown", 1012)
        self.assertTrue(self.auth.finished.is_set())
        self.assertEqual(self.service.connections, 0)
