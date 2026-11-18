"""Real PostgreSQL sessions and Redis admission for WS reauthentication."""

import os
import unittest
from uuid import UUID, uuid4

from auth_test_support import PG_URL, AuthPgFixture
from signaling_test_support import authentication
from test_signaling_transport import WsSession

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


@unittest.skipUnless(PG_URL and REDIS_URL, "real PostgreSQL and Redis URLs are required")
class ReauthenticateIntegrationTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from pydantic import SecretStr
        from zvonilka.api import create_app

        assert REDIS_URL is not None
        self.settings.redis_url = SecretStr(REDIS_URL)
        self.settings.room_registry_key = "zvonilka:reauth-test:" + uuid4().hex
        self.asgi = create_app(self.settings)
        self.app = self.asgi.app
        context = self.app.router.lifespan_context(self.app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.addAsyncCleanup(self.cleanup_registry)
        self.auth = self.app.state.auth
        self.auth.passwords = self.passwords
        owner = await self.auth.register("alice", "Alice", self.password)
        await self.auth.register("bobby", "Bob", self.password)
        self.owner_id = str(owner["id"])
        self.owner = await self.auth.login("alice", self.password)
        self.other = await self.auth.login("bobby", self.password)
        room = await self.app.state.room_repository.create(self.owner_id)
        self.room_id = room["room_id"]
        self.path = f"/api/v1/rooms/{self.room_id}/ws"

    async def cleanup_registry(self):
        await self.app.state.redis.delete(self.settings.room_registry_key)

    async def join(self, ws: WsSession) -> dict:
        await ws.accepted()
        await ws.send(
            {**authentication(UUID(self.room_id)), "access_token": self.owner["access_token"]}
        )
        joined = await ws.receive_wire()
        self.assertEqual(joined["type"], "joined")
        self.assertEqual((await ws.receive_wire())["type"], "participants")
        return joined

    def request(self, joined: dict, token: str) -> dict:
        return {
            "version": 1,
            "type": "reauthenticate",
            "request_id": str(uuid4()),
            "generation": joined["generation"],
            "access_token": token,
        }

    async def test_refreshed_token_for_same_session_keeps_the_connection(self):
        refreshed = await self.auth.refresh(self.owner["refresh_token"])
        expected = self.auth.access.decode_claims(refreshed["access_token"]).expires_at
        async with WsSession(self.asgi, self.path) as ws:
            joined = await self.join(ws)
            request = self.request(joined, refreshed["access_token"])
            await ws.send(request)
            response = await ws.receive_wire()
            self.assertEqual(response["type"], "reauthenticated")
            self.assertEqual(response["request_id"], request["request_id"])
            self.assertEqual(response["generation"], joined["generation"])
            self.assertEqual(response["expires_at"], expected)
            snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
            self.assertEqual(snapshot["members"], [{"user_id": self.owner_id, "online": True}])
        self.assertEqual(self.app.state.signaling.connections, 0)

    async def test_token_of_another_account_is_rejected_and_connection_leaves(self):
        async with WsSession(self.asgi, self.path) as ws:
            joined = await self.join(ws)
            request = self.request(joined, self.other["access_token"])
            await ws.send(request)
            message = await ws.receive_wire()
            self.assertEqual(message["code"], "invalid_access")
            self.assertTrue(message["fatal"])
            self.assertNotIn(self.other["access_token"], str(message))
            self.assertEqual((await ws.event())["type"], "websocket.close")
        snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
        self.assertFalse(snapshot["members"][0]["online"])
        self.assertEqual(self.app.state.signaling.connections, 0)


if __name__ == "__main__":
    unittest.main()
