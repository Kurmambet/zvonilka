"""Actual ASGI WS routing; auth/Redis are explicit dependency doubles."""

import asyncio
import unittest
from time import time
from uuid import UUID, uuid4

from fastapi import FastAPI
from signaling_test_support import StubRooms, TestAccess, TestIdentity, authentication
from starlette.types import Message, Receive, Scope, Send
from test_signaling_transport import WsSession
from zvonilka.auth.errors import AuthError
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import Policy, SignalingService


class TokenAuth:
    def __init__(self):
        self.users = {key: uuid4() for key in ("a", "b", "c")}
        self.session_id = uuid4()
        self.expires_at = int(time()) + 3600

    async def authenticate_access(self, access_token: str) -> TestAccess:
        if access_token not in self.users:
            raise AuthError("invalid_access")
        identity = TestIdentity(self.users[access_token], self.session_id)
        return TestAccess(identity, self.expires_at)


class RelayTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth, self.rooms = TokenAuth(), StubRooms()
        self.service = SignalingService(
            self.auth,
            self.rooms,
            presence_ttl=45,
            policy=Policy(
                auth_timeout=1,
                operation_timeout=1,
                send_timeout=0.15,
                cleanup_timeout=0.2,
                heartbeat_timeout=2,
                heartbeat_min_interval=0.001,
            ),
        )
        self.app = FastAPI()
        self.app.state.signaling = self.service
        self.app.include_router(signaling_router())
        self.room = uuid4()
        self.path = f"/api/v1/rooms/{self.room}/ws"
        self.block_slow = False
        self.blocker = asyncio.Event()

    async def join(self, ws: WsSession, token: str, room: UUID | None = None) -> dict:
        await ws.accepted()
        await ws.send({**authentication(room or self.room), "access_token": token})
        joined = await ws.receive_wire()
        self.assertEqual(joined["type"], "joined")
        self.assertEqual((await ws.receive_wire())["type"], "participants")
        return joined

    async def snapshot(self, ws: WsSession) -> dict:
        value = await ws.receive_wire()
        self.assertEqual(value["type"], "participants")
        return value

    def request(self, first: dict, second: dict, kind: str = "offer") -> dict:
        data = {
            "version": 1,
            "type": kind,
            "request_id": str(uuid4()),
            "generation": first["generation"],
            "target_user_id": second["user_id"],
            "target_generation": second["generation"],
        }
        if kind == "ice_candidate":
            data["candidate"] = {
                "candidate": "candidate:1 1 UDP 1 127.0.0.1 5000 typ host",
                "sdp_mid": "0",
            }
        else:
            data["sdp"] = "v=0\r\nopaque-test-description"
        return data

    async def heartbeat(self, ws: WsSession, joined: dict) -> None:
        await asyncio.sleep(0.002)
        request_id = str(uuid4())
        await ws.send(
            {
                "version": 1,
                "type": "heartbeat",
                "request_id": request_id,
                "generation": joined["generation"],
            }
        )
        ack = await ws.receive_wire()
        self.assertEqual((ack["type"], ack["request_id"]), ("heartbeat_ack", request_id))

    async def slow_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observed(message: Message) -> None:
            if self.block_slow and message["type"] == "websocket.send":
                await self.blocker.wait()
            await send(message)

        await self.app(scope, receive, observed)

    async def test_offer_answer_ice_and_end_are_private_and_ordered(self):
        async with WsSession(self.app, self.path) as a:
            first = await self.join(a, "a")
            async with WsSession(self.app, self.path) as b:
                second = await self.join(b, "b")
                await self.snapshot(a)
                async with WsSession(self.app, self.path) as c:
                    third = await self.join(c, "c")
                    await self.snapshot(a)
                    await self.snapshot(b)
                    for source, dest, own, target, kind in (
                        (a, b, first, second, "offer"),
                        (b, a, second, first, "answer"),
                        (a, b, first, second, "ice_candidate"),
                        (b, a, second, first, "ice_candidate"),
                    ):
                        request = self.request(own, target, kind)
                        await source.send(request)
                        received = await dest.receive_wire()
                        self.assertEqual(received["type"], kind)
                        self.assertEqual(received["request_id"], request["request_id"])
                        self.assertEqual(received["from_user_id"], own["user_id"])
                        self.assertEqual(received["from_generation"], own["generation"])
                        self.assertEqual(received["target_generation"], target["generation"])
                    await a.send(
                        {**self.request(first, second, "ice_candidate"), "candidate": None}
                    )
                    self.assertIsNone((await b.receive_wire())["candidate"])
                    # Raw heartbeat reads also fail if an unintended broadcast/ack arrived.
                    await self.heartbeat(c, third)
                    await self.heartbeat(a, first)
        self.assertEqual(self.service.connections, 0)
        self.assertEqual(self.service.participants.room_count, 0)

    async def test_missing_target_error_keeps_sender_alive(self):
        async with WsSession(self.app, self.path) as a:
            first = await self.join(a, "a")
            request = {**self.request(first, first), "target_user_id": str(uuid4())}
            await a.send(request)
            error = await a.receive_wire()
            self.assertEqual(
                (error["code"], error["fatal"], error["request_id"]),
                ("peer_unavailable", False, request["request_id"]),
            )
            await self.heartbeat(a, first)

    async def test_other_room_target_is_not_reachable(self):
        other = uuid4()
        async with WsSession(self.app, self.path) as a:
            first = await self.join(a, "a")
            async with WsSession(self.app, f"/api/v1/rooms/{other}/ws") as b:
                second = await self.join(b, "b", other)
                await a.send(self.request(first, second))
                self.assertEqual((await a.receive_wire())["code"], "peer_unavailable")
                await self.heartbeat(b, second)
                await self.heartbeat(a, first)

    async def test_old_target_generation_is_rejected_then_new_one_works(self):
        async with WsSession(self.app, self.path) as a:
            first = await self.join(a, "a")
            async with WsSession(self.app, self.path) as old:
                previous = await self.join(old, "b")
                await self.snapshot(a)
                async with WsSession(self.app, self.path) as new:
                    current = await self.join(new, "b")
                    await self.snapshot(a)
                    self.assertEqual((await old.receive_wire())["code"], "connection_replaced")
                    await old.event()
                    assert old.task is not None
                    await asyncio.wait_for(old.task, 2)
                    await a.send(self.request(first, previous))
                    error = await a.receive_wire()
                    self.assertEqual((error["code"], error["fatal"]), ("peer_replaced", False))
                    await a.send(self.request(first, current))
                    self.assertEqual((await new.receive_wire())["type"], "offer")

    async def test_wrong_own_generation_closes_sender_without_forwarding(self):
        async with WsSession(self.app, self.path) as a:
            first = await self.join(a, "a")
            async with WsSession(self.app, self.path) as b:
                second = await self.join(b, "b")
                await self.snapshot(a)
                await a.send({**self.request(first, second), "generation": str(uuid4())})
                error = await a.receive_wire()
                self.assertEqual((error["code"], error["fatal"]), ("stale_generation", True))
                await a.event()
                assert a.task is not None
                await asyncio.wait_for(a.task, 2)
                self.assertEqual(len((await self.snapshot(b))["members"]), 1)
                await self.heartbeat(b, second)

    async def test_forged_sender_field_is_invalid_not_forwarded(self):
        async with WsSession(self.app, self.path) as a:
            first = await self.join(a, "a")
            async with WsSession(self.app, self.path) as b:
                second = await self.join(b, "b")
                await self.snapshot(a)
                await a.send({**self.request(first, second), "from_user_id": second["user_id"]})
                error = await a.receive_wire()
                self.assertEqual((error["code"], error["fatal"]), ("invalid_message", True))
                await a.event()
                assert a.task is not None
                await asyncio.wait_for(a.task, 2)
                await self.snapshot(b)
                await self.heartbeat(b, second)

    async def test_slow_target_does_not_block_another_target(self):
        async with WsSession(self.slow_app, self.path) as b:
            second = await self.join(b, "b")
            async with WsSession(self.app, self.path) as a:
                first = await self.join(a, "a")
                await self.snapshot(b)
                async with WsSession(self.app, self.path) as c:
                    third = await self.join(c, "c")
                    await self.snapshot(a)
                    await self.snapshot(b)
                    self.block_slow = True
                    await a.send(self.request(first, second))
                    await a.send(self.request(first, third))
                    self.assertEqual((await c.receive_wire())["type"], "offer")
                    await self.snapshot(a)
                    await self.snapshot(c)
                    assert b.task is not None
                    await asyncio.wait_for(b.task, 2)
                    self.assertEqual(self.service.connections, 2)
                    await self.heartbeat(a, first)


if __name__ == "__main__":
    unittest.main()
