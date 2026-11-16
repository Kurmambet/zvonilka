"""WS lifecycle through the actual ASGI router, with boundary doubles."""

import asyncio
import unittest
from uuid import uuid4

from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from signaling_test_support import StubAuth, StubRooms, authentication
from test_signaling_transport import WsSession
from zvonilka.http_boundary import SafeHttpBoundary
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import Policy, SignalingService


class PresenceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(
            self.auth,
            self.rooms,
            presence_ttl=45,
            policy=Policy(
                auth_timeout=0.5,
                operation_timeout=0.2,
                send_timeout=0.1,
                cleanup_timeout=0.1,
                heartbeat_timeout=0.3,
                heartbeat_min_interval=0.005,
                shutdown_timeout=2,
            ),
        )
        app = FastAPI()
        app.state.signaling = self.service
        app.include_router(signaling_router())
        self.app = SafeHttpBoundary(app)
        self.room_id = uuid4()
        self.path = f"/api/v1/rooms/{self.room_id}/ws"

    async def joined(self, ws: WsSession) -> dict:
        await ws.accepted()
        await ws.send(authentication(self.room_id))
        result = await ws.receive()
        self.assertEqual(result["type"], "joined")
        self.assertLess(result["heartbeat_interval_ms"], result["heartbeat_timeout_ms"])
        return result

    async def heartbeat(self, ws: WsSession, generation: str) -> dict:
        request_id = str(uuid4())
        await ws.send(
            {
                "version": 1,
                "type": "heartbeat",
                "request_id": request_id,
                "generation": generation,
            }
        )
        response = await ws.receive()
        self.assertEqual(response["request_id"], request_id)
        return response

    async def test_periodic_heartbeat_outlives_initial_window(self):
        async with WsSession(self.app, self.path) as ws:
            joined = await self.joined(ws)
            for _ in range(6):
                await asyncio.sleep(0.08)
                ack = await self.heartbeat(ws, joined["generation"])
                self.assertEqual(ack["type"], "heartbeat_ack")
            self.assertEqual(self.service.connections, 1)
        self.assertEqual(len(self.rooms.heartbeats), 6)
        self.assertEqual(self.service.connections, 0)
        self.assertEqual(self.service.registry_size, 0)

    async def test_replacement_closes_idle_old_socket_without_waiting_for_heartbeat(self):
        async with WsSession(self.app, self.path) as first:
            await self.joined(first)
            async with WsSession(self.app, self.path) as second:
                new = await self.joined(second)
                old_error = await first.receive()
                self.assertEqual(old_error["code"], "connection_replaced")
                self.assertEqual((await first.event())["type"], "websocket.close")
                assert first.task is not None
                await asyncio.wait_for(first.task, timeout=2)
                await asyncio.sleep(0.01)
                ack = await self.heartbeat(second, new["generation"])
                self.assertEqual(ack["type"], "heartbeat_ack")
                self.assertEqual(self.service.registry_size, 1)
        self.assertEqual(self.service.registry_size, 0)
        self.assertEqual(self.rooms.present, {})

    async def test_stale_generation_message_never_renews(self):
        async with WsSession(self.app, self.path) as ws:
            await self.joined(ws)
            error = await self.heartbeat(ws, str(uuid4()))
            self.assertEqual(error["code"], "stale_generation")
            self.assertEqual((await ws.event())["type"], "websocket.close")
        self.assertEqual(self.rooms.heartbeats, [])

    async def test_redis_failure_is_not_a_heartbeat_ack(self):
        async with WsSession(self.app, self.path) as ws:
            joined = await self.joined(ws)
            self.rooms.heartbeat_error = RedisConnectionError("secret")
            await asyncio.sleep(0.01)
            error = await self.heartbeat(ws, joined["generation"])
            self.assertEqual(error["type"], "error")
            self.assertEqual(error["code"], "dependency_unavailable")
            self.assertNotIn("secret", str(error))
        self.assertEqual(self.service.registry_size, 0)

    async def test_heartbeat_timeout_then_leave(self):
        async with WsSession(self.app, self.path) as ws:
            await self.joined(ws)
            error = await ws.receive()
            self.assertEqual(error["code"], "heartbeat_timeout")
            self.assertEqual((await ws.event())["type"], "websocket.close")
        self.assertEqual(self.rooms.present, {})

    async def test_shutdown_includes_authorized_and_unauthorized_sockets(self):
        async with WsSession(self.app, self.path) as authorized:
            await self.joined(authorized)
            async with WsSession(self.app, self.path) as pending:
                await pending.accepted()
                await self.service.shutdown()
                for ws in (authorized, pending):
                    self.assertEqual((await ws.receive())["code"], "server_shutdown")
                    close = await ws.event()
                    self.assertEqual(close["code"], 1012)
                self.assertEqual(self.service.connections, 0)
                self.assertEqual(self.service.registry_size, 0)
        self.assertEqual(self.rooms.present, {})

    async def test_shutdown_cancels_join_and_reaps_waiter_tasks(self):
        self.rooms.join_gate = asyncio.Event()
        async with WsSession(self.app, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            await asyncio.wait_for(self.rooms.join_started.wait(), timeout=2)
            baseline = set(asyncio.all_tasks())
            await self.service.shutdown()
            self.assertEqual((await ws.receive())["code"], "server_shutdown")
            self.assertEqual(self.service.connections, 0)
            await asyncio.sleep(0)
            self.assertTrue(set(asyncio.all_tasks()).issubset(baseline))
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(self.service.registry_size, 0)


if __name__ == "__main__":
    unittest.main()
