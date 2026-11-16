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
from zvonilka.signaling.schemas import Participants
from zvonilka.signaling.service import SignalingService


async def receive_application(ws) -> dict:
    while True:
        message = json.loads(await ws.recv())
        if not isinstance(message, dict):
            raise TypeError("server message must be an object")
        if message.get("type") != "participants":
            return message
        Participants.model_validate(message)


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
                response = await receive_application(ws)
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
                    await receive_application(ws)
                self.assertIsNotNone(caught.exception.rcvd)
                if caught.exception.rcvd is not None:
                    self.assertEqual(caught.exception.rcvd.code, 1009)
        await self.wait_for_cleanup()
        self.assertEqual(self.auth.calls, 0)
        self.assertEqual(self.rooms.joins, [])

    async def test_heartbeat_ack_over_tcp(self):
        async with asyncio.timeout(5):
            async with connect(self.url) as ws:
                await ws.send(json.dumps(authentication(self.room_id)))
                joined = await receive_application(ws)
                self.assertEqual(joined["type"], "joined")
                await asyncio.sleep(0.3)
                request_id = str(uuid4())
                await ws.send(
                    json.dumps(
                        {
                            "version": 1,
                            "type": "heartbeat",
                            "request_id": request_id,
                            "generation": joined["generation"],
                        }
                    )
                )
                ack = await receive_application(ws)
                self.assertEqual(ack["type"], "heartbeat_ack")
                self.assertEqual(ack["request_id"], request_id)
                self.assertEqual(ack["generation"], joined["generation"])
        await self.wait_for_cleanup()
        self.assertEqual(self.service.registry_size, 0)

    async def test_server_stop_closes_live_socket_and_cleans_handler(self):
        async with asyncio.timeout(6):
            async with connect(self.url) as ws:
                await ws.send(json.dumps(authentication(self.room_id)))
                joined = await receive_application(ws)
                self.assertEqual(joined["type"], "joined")
                self.server.should_exit = True
                with self.assertRaises(ConnectionClosed):
                    await receive_application(ws)
            await self.server_task
        self.assertEqual(self.service.connections, 0)
        self.assertEqual(self.service.registry_size, 0)
        self.assertEqual(self.rooms.present, {})


if __name__ == "__main__":
    unittest.main()
