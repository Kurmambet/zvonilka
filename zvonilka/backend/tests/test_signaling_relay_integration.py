"""Real PostgreSQL auth, Redis admission and full-app ASGI signaling relay."""

import os
import unittest
from uuid import UUID, uuid4

from auth_test_support import PG_URL, AuthPgFixture
from signaling_test_support import authentication
from test_signaling_transport import WsSession

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


@unittest.skipUnless(PG_URL and REDIS_URL, "real PostgreSQL and Redis URLs are required")
class RelayIntegrationTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from pydantic import SecretStr
        from zvonilka.api import create_app

        assert REDIS_URL is not None
        self.settings.redis_url = SecretStr(REDIS_URL)
        self.settings.room_registry_key = "zvonilka:relay-test:" + uuid4().hex
        self.asgi = create_app(self.settings)
        self.app = self.asgi.app
        context = self.app.router.lifespan_context(self.app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.addAsyncCleanup(self.cleanup_registry)
        self.app.state.auth.passwords = self.passwords
        auth = self.app.state.auth
        owner = await auth.register("alice", "Alice", self.password)
        guest = await auth.register("bobby", "Bob", self.password)
        self.owner_id, self.guest_id = str(owner["id"]), str(guest["id"])
        self.owner_tokens = await auth.login("alice", self.password)
        self.guest_tokens = await auth.login("bobby", self.password)
        repo = self.app.state.room_repository
        room = await repo.create(self.owner_id)
        self.room_id = room["room_id"]
        invitation = await repo.issue_invite(self.owner_id, self.room_id)
        await repo.redeem(self.guest_id, self.room_id, invitation["token"])
        self.path = f"/api/v1/rooms/{self.room_id}/ws"

    async def cleanup_registry(self):
        await self.app.state.redis.delete(self.settings.room_registry_key)

    async def join(self, ws: WsSession, token: str) -> dict:
        await ws.accepted()
        await ws.send({**authentication(UUID(self.room_id)), "access_token": token})
        joined = await ws.receive_wire()
        self.assertEqual(joined["type"], "joined")
        self.assertEqual((await ws.receive_wire())["type"], "participants")
        return joined

    async def test_real_admission_offer_answer_and_ice(self):
        async with WsSession(self.asgi, self.path) as owner:
            first = await self.join(owner, self.owner_tokens["access_token"])
            async with WsSession(self.asgi, self.path) as guest:
                second = await self.join(guest, self.guest_tokens["access_token"])
                self.assertEqual((await owner.receive_wire())["type"], "participants")
                for source, target, own, dest, kind in (
                    (owner, guest, first, second, "offer"),
                    (guest, owner, second, first, "answer"),
                    (owner, guest, first, second, "ice_candidate"),
                    (guest, owner, second, first, "ice_candidate"),
                ):
                    message = {
                        "version": 1,
                        "type": kind,
                        "request_id": str(uuid4()),
                        "generation": own["generation"],
                        "target_user_id": dest["user_id"],
                        "target_generation": dest["generation"],
                    }
                    if kind == "ice_candidate":
                        message["candidate"] = {
                            "candidate": "candidate:1 1 UDP 1 127.0.0.1 5000 typ host",
                            "sdp_mid": "0",
                        }
                    else:
                        message["sdp"] = "v=0\r\nopaque-integration-description"
                    await source.send(message)
                    received = await target.receive_wire()
                    self.assertEqual(received["type"], kind)
                    self.assertEqual(received["from_user_id"], own["user_id"])
                    self.assertEqual(received["from_generation"], own["generation"])
                    self.assertEqual(received["target_generation"], dest["generation"])
                    self.assertEqual(received["request_id"], message["request_id"])
        snapshot = await self.app.state.room_repository.snapshot(self.owner_id, self.room_id)
        self.assertTrue(all(not member["online"] for member in snapshot["members"]))
        self.assertEqual(self.app.state.signaling.connections, 0)
        self.assertEqual(self.app.state.signaling.participants.room_count, 0)


if __name__ == "__main__":
    unittest.main()
