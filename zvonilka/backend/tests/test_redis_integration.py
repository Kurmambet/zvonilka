import asyncio
import json
import os
import unittest
from uuid import uuid4

from zvonilka.rooms.repository import Limits, RoomError, RoomRepository

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


def uid():
    return str(uuid4())


@unittest.skipUnless(REDIS_URL, "real Redis URL is required")
class RedisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Import failure with an explicitly supplied URL is an error, not a skip.
        from redis.asyncio import Redis

        self.redis = Redis.from_url(
            REDIS_URL, socket_timeout=5, socket_connect_timeout=5, decode_responses=True
        )
        await self.redis.ping()
        self.key = "zvonilka:test:" + uuid4().hex
        self.repo = RoomRepository(self.redis, key=self.key)
        self.owner = uid()
        self.room = (await self.repo.create(self.owner))["room_id"]

    async def test_snapshot_members_have_canonical_order(self):
        guests = [uid(), uid()]

        for guest in guests:
            token = (await self.repo.issue_invite(self.owner, self.room))["token"]
            await self.repo.redeem(guest, self.room, token)

        expected_ids = sorted([self.owner, *guests])

        for attempt in range(10):
            with self.subTest(attempt=attempt):
                snapshot = await self.repo.snapshot(self.owner, self.room)
                actual_ids = [member["user_id"] for member in snapshot["members"]]
                self.assertEqual(actual_ids, expected_ids)

    async def asyncTearDown(self):
        try:
            await self.redis.delete(self.key)
        finally:
            await self.redis.aclose()

    async def error(self, code, call):
        with self.assertRaises(RoomError) as caught:
            await call
        self.assertEqual(caught.exception.code, code)

    async def test_create_and_snapshot(self):
        room = await self.repo.snapshot(self.owner, self.room)
        self.assertEqual(room["state"], "created")
        self.assertEqual(room["members"], [{"user_id": self.owner, "online": False}])

    async def test_unauthorized_snapshot(self):
        await self.error("not_admitted", self.repo.snapshot(uid(), self.room))

    async def test_atomic_room_quota(self):
        entries = [(self.owner, self.room)]
        for _ in range(7):
            owner = uid()
            entries.append((owner, (await self.repo.create(owner))["room_id"]))
        results = await asyncio.gather(
            *(self.repo.join(owner, room, uid()) for owner, room in entries),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(x, dict) for x in results), 2)
        self.assertTrue(
            all(
                isinstance(x, dict) or isinstance(x, RoomError) and x.code == "room_limit"
                for x in results
            )
        )

    async def test_atomic_owner_quota(self):
        other = (await self.repo.create(self.owner))["room_id"]
        results = await asyncio.gather(
            self.repo.join(self.owner, self.room, uid()),
            self.repo.join(self.owner, other, uid()),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(
            [x.code for x in results if isinstance(x, RoomError)], ["owner_room_limit"]
        )

    async def test_invite_concurrent_consumption(self):
        token = (await self.repo.issue_invite(self.owner, self.room))["token"]
        results = await asyncio.gather(
            *(self.repo.redeem(uid(), self.room, token) for _ in range(10)), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(
            sum(isinstance(x, RoomError) and x.code == "invite_consumed" for x in results), 9
        )

    async def test_redeem_lost_response_retry(self):
        guest = uid()
        token = (await self.repo.issue_invite(self.owner, self.room))["token"]
        first = await self.repo.redeem(guest, self.room, token)
        second = await self.repo.redeem(guest, self.room, token)
        self.assertEqual(first, second)
        await self.repo.join(guest, self.room, uid())

    async def test_raw_secret_not_stored(self):
        token = (await self.repo.issue_invite(self.owner, self.room))["token"]
        self.assertNotIn(token, await self.redis.get(self.key))

    async def test_revoke(self):
        from zvonilka.rooms.repository import invite_digest

        token = (await self.repo.issue_invite(self.owner, self.room))["token"]
        await self.repo.revoke_invite(self.owner, self.room, invite_digest(token))
        await self.error("invite_revoked", self.repo.redeem(uid(), self.room, token))

    async def test_invite_limit_and_three_members(self):
        tokens = [(await self.repo.issue_invite(self.owner, self.room))["token"] for _ in range(2)]
        await self.error("invite_limit", self.repo.issue_invite(self.owner, self.room))
        for token in tokens:
            await self.repo.redeem(uid(), self.room, token)
        snapshot = await self.repo.snapshot(self.owner, self.room)
        self.assertEqual(len(snapshot["members"]), 3)

    async def test_generation_fencing(self):
        old, new = uid(), uid()
        await self.repo.join(self.owner, self.room, old)
        result = await self.repo.join(self.owner, self.room, new)
        self.assertEqual(result["replaced_generation"], old)
        await self.error("stale_generation", self.repo.leave(self.owner, self.room, old))
        await self.error("stale_generation", self.repo.heartbeat(self.owner, self.room, old))
        await self.repo.heartbeat(self.owner, self.room, new)
        await self.repo.leave(self.owner, self.room, new)
        self.assertEqual((await self.repo.snapshot(self.owner, self.room))["state"], "recovering")

    async def test_same_generation_retry_no_self_replacement(self):
        generation = uid()
        await self.repo.join(self.owner, self.room, generation)
        retry = await self.repo.join(self.owner, self.room, generation)
        self.assertNotIn("replaced_generation", retry)

    async def test_overload_allows_reconnect(self):
        generation = uid()
        await self.error(
            "overloaded", self.repo.join(self.owner, self.room, generation, overloaded=True)
        )
        await self.repo.join(self.owner, self.room, generation)
        await self.repo.leave(self.owner, self.room, generation)
        await self.repo.join(self.owner, self.room, uid(), overloaded=True)

    async def test_decreased_quota_does_not_evict(self):
        owner = uid()
        room = (await self.repo.create(owner))["room_id"]
        await self.repo.join(self.owner, self.room, uid())
        await self.repo.join(owner, room, uid())
        reduced = RoomRepository(self.redis, key=self.key, limits=Limits(rooms=1))
        await reduced.join(owner, room, uid())
        self.assertEqual((await reduced.snapshot(self.owner, self.room))["state"], "active")

    async def test_expired_presence_and_grace(self):
        short = RoomRepository(self.redis, key=self.key, limits=Limits(presence_ttl=1, grace=1))
        room = await short.create(uid())
        owner = room["owner_id"]
        rid = room["room_id"]
        await short.join(owner, rid, uid())
        await asyncio.sleep(2.2)
        await short.sweep()
        self.assertEqual((await short.snapshot(owner, rid))["state"], "closed")

    async def test_expired_invitation(self):
        short = RoomRepository(self.redis, key=self.key, limits=Limits(invite_ttl=1))
        token = (await short.issue_invite(self.owner, self.room))["token"]
        await asyncio.sleep(1.1)
        await self.error("invite_expired", short.redeem(uid(), self.room, token))

    async def test_pending_expiry(self):
        short = RoomRepository(self.redis, key=self.key, limits=Limits(pending_ttl=1))
        owner = uid()
        room = (await short.create(owner))["room_id"]
        await asyncio.sleep(1.1)
        await short.sweep()
        await self.error("room_closed", short.join(owner, room, uid()))

    async def test_close_idempotence(self):
        await self.error("not_owner", self.repo.close(uid(), self.room))
        first = await self.repo.close(self.owner, self.room)
        second = await self.repo.close(self.owner, self.room)
        self.assertEqual(first, second)
        await self.error("room_closed", self.repo.join(self.owner, self.room, uid()))

    async def test_repository_recreation(self):
        await self.repo.join(self.owner, self.room, uid())
        recreated = RoomRepository(self.redis, key=self.key)
        self.assertEqual((await recreated.snapshot(self.owner, self.room))["state"], "active")

    async def test_registry_bound(self):
        limited = RoomRepository(self.redis, key=self.key, limits=Limits(max_records=1))
        await self.error("registry_full", limited.create(uid()))

    async def test_schema_mismatch_does_not_overwrite(self):
        raw = json.dumps({"schema": 999, "rooms": {}})
        await self.redis.set(self.key, raw)
        await self.error("schema_mismatch", self.repo.sweep())
        self.assertEqual(await self.redis.get(self.key), raw)


if __name__ == "__main__":
    unittest.main()
