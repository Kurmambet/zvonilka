"""Actual auth/Redis admission through the full app's ASGI WS interface."""

import asyncio
import os
import unittest
from dataclasses import replace
from uuid import uuid4

from auth_test_support import PG_URL, AuthPgFixture
from test_signaling_transport import WsSession
from zvonilka.rooms.repository import RoomError
from zvonilka.signaling.service import Policy, SignalingService

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

    def short_presence(self):
        repo = self.app.state.room_repository
        repo.limits = replace(repo.limits, presence_ttl=1)
        self.app.state.signaling = SignalingService(
            self.app.state.auth,
            repo,
            presence_ttl=1,
            policy=Policy(heartbeat_timeout=0.4, heartbeat_min_interval=0.01),
        )
        return repo

    async def send_heartbeat(self, ws: WsSession, generation: str):
        request_id = str(uuid4())
        await ws.send(
            {
                "version": 1,
                "type": "heartbeat",
                "request_id": request_id,
                "generation": generation,
            }
        )
        ack = await ws.receive()
        self.assertEqual(ack["type"], "heartbeat_ack")
        self.assertEqual(ack["request_id"], request_id)

    async def test_heartbeat_keeps_real_lease_alive_beyond_original_ttl(self):
        repo = self.short_presence()
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            joined = await ws.receive()
            self.assertEqual(joined["type"], "joined")
            for _ in range(14):
                await asyncio.sleep(0.1)
                await self.send_heartbeat(ws, joined["generation"])
            snapshot = await repo.snapshot(self.owner_id, self.room_id)
            self.assertTrue(snapshot["members"][0]["online"])
        self.assertFalse((await repo.snapshot(self.owner_id, self.room_id))["members"][0]["online"])

    async def test_old_disconnect_and_late_heartbeat_do_not_remove_new_presence(self):
        repo = self.app.state.room_repository
        async with WsSession(self.asgi, self.path) as first:
            await first.accepted()
            await first.send(self.payload())
            old = await first.receive()
            async with WsSession(self.asgi, self.path) as second:
                await second.accepted()
                await second.send(self.payload())
                new = await second.receive()
                self.assertNotEqual(new["generation"], old["generation"])
                self.assertEqual((await first.receive())["code"], "connection_replaced")
                await first.event()
                assert first.task is not None
                await asyncio.wait_for(first.task, timeout=3)
                with self.assertRaises(RoomError) as caught:
                    await repo.heartbeat(self.owner_id, self.room_id, old["generation"])
                self.assertEqual(caught.exception.code, "stale_generation")
                snapshot = await repo.snapshot(self.owner_id, self.room_id)
                self.assertTrue(snapshot["members"][0]["online"])

    async def test_shutdown_cleans_real_presence_before_return(self):
        repo = self.app.state.room_repository
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            self.assertEqual((await ws.receive())["type"], "joined")
            await self.app.state.signaling.shutdown()
            self.assertEqual((await ws.receive())["code"], "server_shutdown")
            snapshot = await repo.snapshot(self.owner_id, self.room_id)
            self.assertFalse(snapshot["members"][0]["online"])
        self.assertEqual(self.app.state.signaling.connections, 0)

    async def test_no_heartbeat_releases_real_presence(self):
        repo = self.short_presence()
        async with WsSession(self.asgi, self.path) as ws:
            await ws.accepted()
            await ws.send(self.payload())
            self.assertEqual((await ws.receive())["type"], "joined")
            error = await ws.receive()
            self.assertEqual(error["code"], "heartbeat_timeout")
            self.assertEqual((await ws.event())["type"], "websocket.close")
        snapshot = await repo.snapshot(self.owner_id, self.room_id)
        self.assertFalse(snapshot["members"][0]["online"])
        self.assertEqual(self.app.state.signaling.registry_size, 0)


if __name__ == "__main__":
    unittest.main()
