"""Single writer ownership and send-failure cleanup through the actual ASGI router."""

import asyncio
import unittest
from uuid import uuid4

from fastapi import FastAPI
from signaling_test_support import StubAuth, StubRooms, authentication
from starlette.types import Message, Receive, Scope, Send
from test_signaling_transport import WsSession
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import Policy, SignalingService


class WriterTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(
            self.auth,
            self.rooms,
            presence_ttl=45,
            policy=Policy(send_timeout=0.1, heartbeat_min_interval=0.005),
        )
        self.app = FastAPI()
        self.app.state.signaling = self.service
        self.app.include_router(signaling_router())
        self.room_id = uuid4()
        self.path = f"/api/v1/rooms/{self.room_id}/ws"
        self.owners: set[object] = set()
        self.fail_send = False
        self.block_send = False
        self.blocker = asyncio.Event()

    def wrapped_send(self, send: Send) -> Send:
        async def observed(message: Message) -> None:
            if message["type"] in {"websocket.send", "websocket.close"}:
                self.owners.add(asyncio.current_task())
            if message["type"] == "websocket.send":
                if self.fail_send:
                    raise OSError("secret")
                if self.block_send:
                    await self.blocker.wait()
            await send(message)

        return observed

    async def observed_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, self.wrapped_send(send))

    async def test_join_heartbeat_and_close_have_one_writer(self):
        async with WsSession(self.observed_app, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            joined = await ws.receive()
            self.assertEqual(joined["type"], "joined")
            await asyncio.sleep(0.01)
            request_id = str(uuid4())
            await ws.send(
                {
                    "version": 1,
                    "type": "heartbeat",
                    "request_id": request_id,
                    "generation": joined["generation"],
                }
            )
            ack = await ws.receive()
            self.assertEqual(ack["type"], "heartbeat_ack")
            self.assertEqual(ack["request_id"], request_id)
        self.assertEqual(len(self.owners), 1)
        self.assertEqual(self.service.connections, 0)
        self.assertEqual(self.service.registry_size, 0)

    async def test_send_failure_releases_presence_without_waiting_for_heartbeat(self):
        self.fail_send = True

        async with WsSession(self.observed_app, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))

            assert ws.task is not None
            await asyncio.wait_for(ws.task, timeout=2)

            # Проверяем до выхода из контекста: __aexit__ ещё не отправлял disconnect.
            self.assertTrue(ws.task.done())
            self.assertFalse(ws.task.cancelled())
            self.assertEqual(self.service.connections, 0)
            self.assertEqual(self.service.registry_size, 0)
            self.assertEqual(self.rooms.present, {})
            self.assertEqual(self.rooms.leaves, self.rooms.joins)
            self.assertEqual(len(self.rooms.joins), 1)
            self.assertEqual(len(self.owners), 1)

    async def test_slow_send_times_out_and_handler_finishes(self):
        self.block_send = True
        async with WsSession(self.observed_app, self.path) as ws:
            await ws.accepted()
            await ws.send(authentication(self.room_id))
            event = await ws.event()
            self.assertEqual(event["type"], "websocket.close")
            self.assertEqual(event["code"], 1013)
        self.assertEqual(self.service.connections, 0)
        self.assertEqual(self.rooms.present, {})
        self.assertEqual(len(self.owners), 1)


if __name__ == "__main__":
    unittest.main()
