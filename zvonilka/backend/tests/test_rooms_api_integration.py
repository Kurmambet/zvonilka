import asyncio
import json
import os
import unittest
from dataclasses import replace
from uuid import uuid4

from auth_test_support import PG_URL, AuthPgFixture

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


@unittest.skipUnless(PG_URL and REDIS_URL, "real PostgreSQL and Redis URLs are required")
class RoomApiTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        import httpx
        from pydantic import SecretStr
        from zvonilka.api import create_app

        assert REDIS_URL is not None
        self.settings.redis_url = SecretStr(REDIS_URL)
        self.settings.room_registry_key = "zvonilka:room_api_test:" + uuid4().hex
        self.asgi = create_app(self.settings)
        self.app = self.asgi.app
        context = self.app.router.lifespan_context(self.app)
        await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.addAsyncCleanup(self.cleanup_redis)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.asgi), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        # Real Argon2, but cheaper parameters for test speed. Separate password
        # tests cover production cost and worker-thread execution.
        self.app.state.auth.passwords = self.passwords
        self.owner_id, self.owner = await self.user("alice")

    async def cleanup_redis(self):
        redis = self.app.state.redis
        keys = [key async for key in redis.scan_iter(match=self.settings.rate_limit_prefix + "*")]
        await redis.delete(self.settings.room_registry_key, *keys)

    async def user(self, username):
        auth = self.app.state.auth
        profile = await auth.register(username, username.title(), self.password)
        pair = await auth.login(username, self.password)
        return str(profile["id"]), {"Authorization": "Bearer " + pair["access_token"]}

    async def room(self, headers=None):
        response = await self.client.post("/api/v1/rooms", headers=headers or self.owner, json={})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def invite(self, room_id):
        response = await self.client.post(f"/api/v1/rooms/{room_id}/invites", headers=self.owner)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def redeem(self, invitation, headers):
        return await self.client.post(
            "/api/v1/invites/redeem", headers=headers, json={"invitation": invitation}
        )

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("request_id", response.json()["error"])

    async def test_unauthenticated_requests_cannot_mutate_rooms(self):
        from zvonilka.rooms.invitations import encode_invitation

        rid = str(uuid4())
        requests = [
            ("POST", "/api/v1/rooms", {}),
            ("GET", f"/api/v1/rooms/{rid}", None),
            ("POST", f"/api/v1/rooms/{rid}/invites", None),
            ("DELETE", f"/api/v1/rooms/{rid}/invites/" + "a" * 64, None),
            ("DELETE", f"/api/v1/rooms/{rid}", None),
            (
                "POST",
                "/api/v1/invites/redeem",
                {"invitation": encode_invitation(uuid4(), "A" * 43)},
            ),
        ]
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, path, json=body)
                self.assert_error(response, 401, "invalid_access")
        self.assertFalse(await self.app.state.redis.exists(self.settings.room_registry_key))

    async def test_create_and_snapshot(self):
        room = await self.room()
        self.assertEqual(room["owner_id"], self.owner_id)
        self.assertEqual(room["state"], "created")
        self.assertEqual(room["members"], [{"user_id": self.owner_id, "online": False}])
        response = await self.client.get(f"/api/v1/rooms/{room['room_id']}", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), room)

    async def test_cannot_spoof_owner_in_create_body(self):
        response = await self.client.post(
            "/api/v1/rooms", headers=self.owner, json={"owner_id": str(uuid4())}
        )
        self.assert_error(response, 422, "invalid_request")
        self.assertFalse(await self.app.state.redis.exists(self.settings.room_registry_key))

    async def test_other_account_cannot_read_room(self):
        room = await self.room()
        _, stranger = await self.user("bobby")
        response = await self.client.get(f"/api/v1/rooms/{room['room_id']}", headers=stranger)
        self.assert_error(response, 403, "not_admitted")

    async def test_pending_limit(self):
        for _ in range(3):
            await self.room()
        response = await self.client.post("/api/v1/rooms", headers=self.owner, json={})
        self.assert_error(response, 409, "pending_limit")

    async def test_issue_delivers_secret_once_and_stores_only_digest(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        token = invitation["invitation"].rsplit(".", 1)[1]
        raw = await self.app.state.redis.get(self.settings.room_registry_key)
        self.assertNotIn(token, raw)
        self.assertIn(invitation["invite_id"], raw)
        self.assertEqual(invitation["room_id"], room["room_id"])
        snapshot = await self.client.get(f"/api/v1/rooms/{room['room_id']}", headers=self.owner)
        self.assertNotIn(token, snapshot.text)
        self.assertEqual(snapshot.json()["state"], "created")

    async def test_redeem_and_lost_response_retry(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        guest_id, guest = await self.user("bobby")
        first = await self.redeem(invitation["invitation"], guest)
        second = await self.redeem(invitation["invitation"], guest)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual({m["user_id"] for m in first.json()["members"]}, {self.owner_id, guest_id})
        self.assertEqual(first.json()["state"], "created")
        self.assertTrue(all(not m["online"] for m in first.json()["members"]))

    async def test_two_accounts_cannot_consume_same_invitation(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        _, first = await self.user("bobby")
        _, second = await self.user("carol")
        results = await asyncio.gather(
            self.redeem(invitation["invitation"], first),
            self.redeem(invitation["invitation"], second),
        )
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        failure = next(r for r in results if r.status_code != 200)
        self.assert_error(failure, 409, "invite_consumed")

    async def test_admitted_guest_does_not_gain_owner_permissions(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        _, guest = await self.user("bobby")
        self.assertEqual((await self.redeem(invitation["invitation"], guest)).status_code, 200)
        path = f"/api/v1/rooms/{room['room_id']}"
        responses = [
            await self.client.post(path + "/invites", headers=guest),
            await self.client.delete(path + "/invites/" + invitation["invite_id"], headers=guest),
            await self.client.delete(path, headers=guest),
        ]
        for response in responses:
            self.assert_error(response, 403, "not_owner")

    async def test_revoke_blocks_redeem_and_new_invitation_works(self):
        room = await self.room()
        old = await self.invite(room["room_id"])
        response = await self.client.delete(
            f"/api/v1/rooms/{room['room_id']}/invites/{old['invite_id']}", headers=self.owner
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        _, guest = await self.user("bobby")
        self.assert_error(await self.redeem(old["invitation"], guest), 410, "invite_revoked")
        new = await self.invite(room["room_id"])
        self.assertNotEqual(old["invitation"], new["invitation"])
        self.assertEqual((await self.redeem(new["invitation"], guest)).status_code, 200)

    async def test_close_is_idempotent_and_blocks_new_redeem(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        path = f"/api/v1/rooms/{room['room_id']}"
        for _ in range(2):
            response = await self.client.delete(path, headers=self.owner)
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.content, b"")
        _, guest = await self.user("bobby")
        self.assert_error(await self.redeem(invitation["invitation"], guest), 410, "room_closed")
        snapshot = await self.client.get(path, headers=self.owner)
        self.assertEqual(snapshot.json()["state"], "closed")

    async def test_invite_limit_and_three_members(self):
        room = await self.room()
        invitations = [await self.invite(room["room_id"]) for _ in range(2)]
        response = await self.client.post(
            f"/api/v1/rooms/{room['room_id']}/invites", headers=self.owner
        )
        self.assert_error(response, 409, "invite_limit")
        for name, invitation in zip(("bobby", "carol"), invitations, strict=True):
            _, guest = await self.user(name)
            response = await self.redeem(invitation["invitation"], guest)
            self.assertEqual(response.status_code, 200, response.text)
        snapshot = await self.client.get(f"/api/v1/rooms/{room['room_id']}", headers=self.owner)
        self.assertEqual(len(snapshot.json()["members"]), 3)

    async def test_copying_text_or_get_request_never_consumes_invite(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        response = await self.client.get("/api/v1/invites/redeem", headers=self.owner)
        self.assert_error(response, 405, "method_not_allowed")
        _, guest = await self.user("bobby")
        self.assertEqual((await self.redeem(invitation["invitation"], guest)).status_code, 200)

    async def test_invalid_invitation_never_echoes_secret(self):
        secret = "TOP_SECRET_INVALID_INVITATION"
        response = await self.redeem(secret, self.owner)
        self.assert_error(response, 422, "invalid_request")
        self.assertNotIn(secret, response.text)

    async def test_redeem_cannot_override_actor(self):
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        guest_id, guest = await self.user("bobby")
        response = await self.client.post(
            "/api/v1/invites/redeem",
            headers=guest,
            json={"invitation": invitation["invitation"], "user_id": self.owner_id},
        )
        self.assert_error(response, 422, "invalid_request")
        successful = await self.redeem(invitation["invitation"], guest)
        self.assertEqual(successful.status_code, 200)
        self.assertIn(guest_id, {m["user_id"] for m in successful.json()["members"]})

    async def test_revoked_session_cannot_create_room(self):
        await self.client.post("/api/v1/auth/logout", headers=self.owner)
        response = await self.client.post("/api/v1/rooms", headers=self.owner, json={})
        self.assert_error(response, 401, "invalid_access")

    async def test_rate_limit_counts_failed_redemption_and_is_per_account(self):
        from zvonilka.rooms.invitations import encode_invitation

        self.settings.invite_redeem_rate_limit = 2
        invitation = encode_invitation(uuid4(), "A" * 43)
        for _ in range(2):
            self.assert_error(await self.redeem(invitation, self.owner), 404, "room_not_found")
        self.assert_error(await self.redeem(invitation, self.owner), 429, "rate_limited")
        _, guest = await self.user("bobby")
        self.assert_error(await self.redeem(invitation, guest), 404, "room_not_found")

    async def test_unknown_redis_schema_returns_safe_503(self):
        from zvonilka.observability.logging import SafeJsonFormatter

        redis = self.app.state.redis
        key = self.settings.room_registry_key
        original = json.dumps({"schema": 999, "rooms": {}})
        await redis.set(key, original)

        with self.assertLogs("zvonilka.api", level="ERROR") as captured:
            response = await self.client.post(
                "/api/v1/rooms",
                headers=self.owner,
                json={},
            )

        self.assert_error(response, 503, "schema_mismatch")
        self.assertNotIn(self.password, response.text)

        # Rejection must not overwrite an unsupported aggregate.
        self.assertEqual(await redis.get(key), original)

        records = [
            record
            for record in captured.records
            if getattr(record, "safe_fields", {}).get("code") == "schema_mismatch"
        ]
        self.assertEqual(len(records), 1)

        rendered = SafeJsonFormatter().format(records[0])
        event = json.loads(rendered)

        self.assertEqual(event["event"], "dependency_unavailable")
        self.assertEqual(event["code"], "schema_mismatch")
        self.assertEqual(event["dependency"], "redis")
        self.assertNotIn("Traceback", rendered)
        self.assertNotIn(self.password, rendered)
        self.assertEqual(len(rendered.splitlines()), 1)

    async def test_expired_invitation(self):
        repo = self.app.state.room_repository
        repo.limits = replace(repo.limits, invite_ttl=1)
        room = await self.room()
        invitation = await self.invite(room["room_id"])
        _, guest = await self.user("bobby")
        await asyncio.sleep(1.15)
        self.assert_error(await self.redeem(invitation["invitation"], guest), 410, "invite_expired")

    async def test_invalid_path_values(self):
        response = await self.client.get("/api/v1/rooms/not-a-uuid", headers=self.owner)
        self.assert_error(response, 422, "invalid_request")
        room = await self.room()
        response = await self.client.delete(
            f"/api/v1/rooms/{room['room_id']}/invites/not-a-digest", headers=self.owner
        )
        self.assert_error(response, 422, "invalid_request")

    async def test_http_create_and_redeem_do_not_activate_rooms(self):
        repo = self.app.state.room_repository
        repo.limits = replace(repo.limits, rooms=1)
        active = await self.room()
        await repo.join(self.owner_id, active["room_id"], str(uuid4()))
        pending = await self.room()
        invitation = await self.invite(pending["room_id"])
        _, guest = await self.user("bobby")
        redeemed = await self.redeem(invitation["invitation"], guest)
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        self.assertEqual(redeemed.json()["state"], "created")
