"""Real ASGI WS routing for reauthentication; auth and Redis are dependency doubles."""

import asyncio
import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from signaling_test_support import StubAuth, StubRooms, TestAccess, authentication
from sqlalchemy.exc import OperationalError
from test_signaling_transport import WsSession
from zvonilka.signaling.router import signaling_router
from zvonilka.signaling.service import Policy, SignalingService


class HangingAuth(StubAuth):
    def __init__(self):
        super().__init__()
        self.hold = False

    async def authenticate_access(self, access_token: str) -> TestAccess:
        if self.hold:
            await asyncio.Event().wait()
        return await super().authenticate_access(access_token)


class ReauthenticateTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth, self.rooms = HangingAuth(), StubRooms()
        self.room_id = uuid4()
        self.path = f"/api/v1/rooms/{self.room_id}/ws"
        self.build(reauth_min_interval=0.001)

    def build(self, **policy) -> None:
        self.service = SignalingService(
            self.auth,
            self.rooms,
            presence_ttl=45,
            policy=Policy(
                auth_timeout=1,
                operation_timeout=0.2,
                send_timeout=0.15,
                cleanup_timeout=0.2,
                heartbeat_timeout=2,
                heartbeat_min_interval=0.001,
                **policy,
            ),
        )
        self.app = FastAPI()
        self.app.state.signaling = self.service
        self.app.include_router(signaling_router())

    async def join(self, ws: WsSession, user_id: UUID | None = None) -> dict:
        if user_id is not None:
            self.auth.user_id = user_id
        await ws.accepted()
        await ws.send(authentication(self.room_id))
        joined = await ws.receive_wire()
        self.assertEqual(joined["type"], "joined")
        self.assertEqual((await ws.receive_wire())["type"], "participants")
        return joined

    def request(self, joined: dict, token: str = "next-token") -> dict:
        return {
            "version": 1,
            "type": "reauthenticate",
            "request_id": str(uuid4()),
            "generation": joined["generation"],
            "access_token": token,
        }

    async def assert_fatal(self, ws: WsSession, code: str, request: dict | None = None) -> dict:
        message = await ws.receive_wire()
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["code"], code)
        self.assertTrue(message["fatal"])
        if request is not None:
            self.assertEqual(message["request_id"], request["request_id"])
        self.assertNotIn("next-token", json.dumps(message))
        self.assertEqual((await ws.event())["type"], "websocket.close")
        return message

    async def test_success_keeps_same_socket_generation_and_presence(self):
        self.auth.grant("next-token", expires_at=self.auth.expires_at + 90)
        async with WsSession(self.app, self.path) as ws:
            joined = await self.join(ws)
            request = self.request(joined)
            await ws.send(request)
            response = await ws.receive_wire()
            self.assertEqual(response["type"], "reauthenticated")
            self.assertEqual(response["request_id"], request["request_id"])
            self.assertEqual(response["generation"], joined["generation"])
            self.assertEqual(response["expires_at"], self.auth.expires_at + 90)
            await asyncio.sleep(0.01)
            await ws.send(
                {
                    "version": 1,
                    "type": "heartbeat",
                    "request_id": str(uuid4()),
                    "generation": joined["generation"],
                }
            )
            self.assertEqual((await ws.receive_wire())["type"], "heartbeat_ack")
            self.assertEqual(len(self.rooms.joins), 1)
            self.assertEqual(self.rooms.leaves, [])
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(self.service.connections, 0)

    async def test_invalid_other_account_and_old_expiry_are_fatal(self):
        self.auth.grant("other-user", user_id=uuid4())
        self.auth.grant("other-session", session_id=uuid4())
        self.auth.grant("older", expires_at=self.auth.expires_at - 1)
        for token in ("unknown", "other-user", "other-session", "older"):
            with self.subTest(token=token):
                async with WsSession(self.app, self.path) as ws:
                    joined = await self.join(ws)
                    request = self.request(joined, token)
                    await ws.send(request)
                    await self.assert_fatal(ws, "invalid_access", request)
                self.assertEqual(self.service.connections, 0)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_wrong_generation_is_fatal_without_database_check(self):
        async with WsSession(self.app, self.path) as ws:
            joined = await self.join(ws)
            calls = self.auth.calls
            request = {**self.request(joined), "generation": str(uuid4())}
            await ws.send(request)
            await self.assert_fatal(ws, "stale_generation", request)
            self.assertEqual(self.auth.calls, calls)

    async def test_frequent_requests_are_rate_limited(self):
        self.build(reauth_min_interval=60)
        self.auth.grant("next-token")
        async with WsSession(self.app, self.path) as ws:
            joined = await self.join(ws)
            await ws.send(self.request(joined))
            self.assertEqual((await ws.receive_wire())["type"], "reauthenticated")
            second = self.request(joined)
            await ws.send(second)
            await self.assert_fatal(ws, "rate_limited", second)

    async def test_database_failure_and_timeout_are_safe_fatal_errors(self):
        for mode in ("failure", "timeout"):
            with self.subTest(mode=mode):
                self.auth.error = None
                self.auth.hold = False
                async with WsSession(self.app, self.path) as ws:
                    joined = await self.join(ws)
                    if mode == "failure":
                        self.auth.error = OperationalError(
                            "SELECT 1", None, Exception("next-token private detail")
                        )
                    else:
                        self.auth.hold = True
                    request = self.request(joined)
                    await ws.send(request)
                    message = await self.assert_fatal(ws, "dependency_unavailable", request)
                    self.assertNotIn("private", json.dumps(message))
                self.assertEqual(self.service.connections, 0)
        self.auth.error, self.auth.hold = None, False
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_relay_still_works_after_reauthentication(self):
        first_user, second_user = uuid4(), uuid4()
        self.auth.grant("next-token", user_id=first_user)
        async with WsSession(self.app, self.path) as first:
            first_joined = await self.join(first, first_user)
            async with WsSession(self.app, self.path) as second:
                second_joined = await self.join(second, second_user)
                self.assertEqual((await first.receive_wire())["type"], "participants")
                await first.send(self.request(first_joined))
                self.assertEqual((await first.receive_wire())["type"], "reauthenticated")
                offer = {
                    "version": 1,
                    "type": "offer",
                    "request_id": str(uuid4()),
                    "generation": first_joined["generation"],
                    "target_user_id": second_joined["user_id"],
                    "target_generation": second_joined["generation"],
                    "sdp": "v=0\r\nopaque-reauth-test",
                }
                await first.send(offer)
                relayed = await second.receive_wire()
                self.assertEqual(relayed["type"], "offer")
                self.assertEqual(relayed["from_generation"], first_joined["generation"])
                self.assertEqual(relayed["request_id"], offer["request_id"])

    async def test_grace_notifies_once_and_closes_idle_socket(self):
        self.build(reauth_grace_seconds=0.3)
        async with WsSession(self.app, self.path) as ws:
            with patch("zvonilka.signaling.service.time", return_value=self.auth.expires_at - 0.05):
                joined = await self.join(ws)
            notice = await ws.receive_wire()
            self.assertEqual(notice["type"], "reauth_required")
            self.assertEqual(notice["generation"], joined["generation"])
            await self.assert_fatal(ws, "reauthentication_timeout")
        self.assertEqual(self.rooms.leaves, self.rooms.joins)
        self.assertEqual(self.service.connections, 0)

    async def test_grace_preserves_heartbeat_and_blocks_relay_both_ways_until_renewal(self):
        first_user, second_user = uuid4(), uuid4()
        self.auth.grant("next-token", user_id=first_user)
        async with WsSession(self.app, self.path) as first:
            with patch("zvonilka.signaling.service.time", return_value=self.auth.expires_at - 0.1):
                a = await self.join(first, first_user)
            self.assertEqual((await first.receive_wire())["type"], "reauth_required")
            async with WsSession(self.app, self.path) as second:
                b = await self.join(second, second_user)
                self.assertEqual((await first.receive_wire())["type"], "participants")
                await first.send(
                    {
                        "version": 1,
                        "type": "heartbeat",
                        "request_id": str(uuid4()),
                        "generation": a["generation"],
                    }
                )
                self.assertEqual((await first.receive_wire())["type"], "heartbeat_ack")

                async def offer(ws: WsSession, own: dict, target: dict) -> None:
                    await ws.send(
                        {
                            "version": 1,
                            "type": "offer",
                            "request_id": str(uuid4()),
                            "generation": own["generation"],
                            "target_user_id": target["user_id"],
                            "target_generation": target["generation"],
                            "sdp": "v=0",
                        }
                    )

                for ws, own, target, code in (
                    (first, a, b, "authorization_expired"),
                    (second, b, a, "peer_unavailable"),
                ):
                    await offer(ws, own, target)
                    error = await ws.receive_wire()
                    self.assertEqual(error["code"], code)
                    self.assertFalse(error["fatal"])
                await first.send(self.request(a))
                renewed = await first.receive_wire()
                self.assertEqual(renewed["type"], "reauthenticated")
                self.assertEqual(renewed["generation"], a["generation"])
                await offer(second, b, a)
                self.assertEqual((await first.receive_wire())["type"], "offer")
                await offer(first, a, b)
                self.assertEqual((await second.receive_wire())["type"], "offer")


if __name__ == "__main__":
    unittest.main()
