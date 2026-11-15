"""Actual auth/Redis admission through the full app's ASGI WS interface."""

import os
import unittest
from uuid import uuid4

from auth_test_support import PG_URL, AuthPgFixture
from test_signaling_transport import WsSession

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


@unittest.skipUnless(PG_URL and REDIS_URL, "real PostgreSQL and Redis URLs are required")
class SignalingIntegrationTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from pydantic import SecretStr
        from zvonilka.api import create_app

        assert REDIS_URL is not None
        self.settings.redis_url = SecretStr(REDIS_URL)
        self.settings.room_registry_key = "zvonilka:ws-test:" + uuid4().hex
        self.asgi = create_app(self.settings)
        self.app = self.asgi.app
        context = self.app.router.lifespan_context(self.app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.addAsyncCleanup(self.cleanup_registry)
        self.app.state.auth.passwords = self.passwords
        profile = await self.app.state.auth.register("alice", "Alice", self.password)
        self.owner_id = str(profile["id"])
        pair = await self.app.state.auth.login("alice", self.password)
        self.token = pair["access_token"]
        self.room = await self.app.state.room_repository.create(self.owner_id)
        self.room_id = self.room["room_id"]
        self.path = f"/api/v1/rooms/{self.room_id}/ws"

    async def cleanup_registry(self):
        await self.app.state.redis.delete(self.settings.room_registry_key)

    def payload(self) -> dict:
        return {
            "version": 1,
            "type": "authenticate",
            "request_id": str(uuid4()),
            "room_id": self.room_id,
            "access_token": self.token,
            "app_version": "0.5.0-dev.1",
            "capabilities": [],
        }

    async def test_join_activates_and_disconnect_leaves_real_presence(self):
        repo = self.app.state.room_repository
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            joined = await ws.receive()
            self.assertEqual(joined["type"], "joined")
            snapshot = await repo.snapshot(self.owner_id, self.room_id)
            self.assertEqual(snapshot["state"], "active")
            self.assertEqual(snapshot["members"], [{"user_id": self.owner_id, "online": True}])
        snapshot = await repo.snapshot(self.owner_id, self.room_id)
        self.assertEqual(snapshot["state"], "recovering")
        self.assertFalse(snapshot["members"][0]["online"])
        self.assertEqual(self.app.state.signaling.connections, 0)

    async def test_invalid_token_does_not_activate_room(self):
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send({**self.payload(), "access_token": "wrong"})
            self.assertEqual((await ws.receive())["code"], "invalid_access")
        snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
        self.assertEqual(snapshot["state"], "created")
        self.assertFalse(snapshot["members"][0]["online"])

    async def test_foreign_account_cannot_join(self):
        await self.app.state.auth.register("bobby", "Bob", self.password)
        pair = await self.app.state.auth.login("bobby", self.password)
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send({**self.payload(), "access_token": pair["access_token"]})
            self.assertEqual((await ws.receive())["code"], "not_admitted")
        snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
        self.assertEqual(snapshot["state"], "created")

    async def test_schema_mismatch_is_safe_and_does_not_mutate_redis(self):
        redis = self.app.state.redis
        raw = '{"schema":999,"rooms":{}}'
        await redis.set(self.settings.room_registry_key, raw)
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            response = await ws.receive()
            self.assertEqual(response["code"], "dependency_unavailable")
            self.assertNotIn(self.token, str(response))
        self.assertEqual(await redis.get(self.settings.room_registry_key), raw)


if __name__ == "__main__":
    unittest.main()
