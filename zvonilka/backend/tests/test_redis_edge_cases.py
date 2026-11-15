"""Additional real-Redis invariants; no simulated Redis implementation."""
import asyncio
import os
import unittest
from uuid import uuid4

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


def uid():
    return str(uuid4())


@unittest.skipUnless(REDIS_URL, "real Redis URL is required")
class RedisEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from redis.asyncio import Redis
        from zvonilka.rooms.repository import RoomRepository, Limits, RoomError
        self.Repository, self.Limits, self.RoomError = RoomRepository, Limits, RoomError
        self.redis = Redis.from_url(REDIS_URL, decode_responses=True,
                                    socket_timeout=5, socket_connect_timeout=5)
        await self.redis.ping()
        self.key = "zvonilka:edge:" + uuid4().hex
        self.repo = RoomRepository(self.redis, key=self.key)
        self.owner = uid()
        self.room = (await self.repo.create(self.owner))["room_id"]

    async def asyncTearDown(self):
        try:
            await self.redis.delete(self.key)
        finally:
            await self.redis.aclose()

    def configured(self, **limits):
        return self.Repository(self.redis, key=self.key, limits=self.Limits(**limits))

    async def assert_error(self, code, awaitable):
        with self.assertRaises(self.RoomError) as ctx:
            await awaitable
        self.assertEqual(ctx.exception.code, code)

    async def test_concurrent_pending_creation_cannot_exceed_limit(self):
        results = await asyncio.gather(
            *(self.repo.create(self.owner) for _ in range(12)), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 2)
        self.assertEqual(sum(isinstance(r, self.RoomError) and r.code == "pending_limit"
                             for r in results), 10)

    async def test_concurrent_invite_issue_cannot_overallocate(self):
        results = await asyncio.gather(
            *(self.repo.issue_invite(self.owner, self.room) for _ in range(12)),
            return_exceptions=True)
        issued = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(issued), 2)
        self.assertEqual(len({r["token"] for r in issued}), 2)
        self.assertEqual(sum(isinstance(r, self.RoomError) and r.code == "invite_limit"
                             for r in results), 10)

    async def test_no_capacity_rejection_does_not_consume_token(self):
        tokens = [(await self.repo.issue_invite(self.owner, self.room))["token"]
                  for _ in range(2)]
        first, second = uid(), uid()
        reduced = self.configured(participants=2)
        await reduced.redeem(first, self.room, tokens[0])
        await self.assert_error("participant_limit", reduced.redeem(second, self.room, tokens[1]))
        snapshot = await self.repo.redeem(second, self.room, tokens[1])
        self.assertEqual({m["user_id"] for m in snapshot["members"]},
                         {self.owner, first, second})

    async def test_concurrent_different_tokens_last_slot(self):
        tokens = [(await self.repo.issue_invite(self.owner, self.room))["token"]
                  for _ in range(2)]
        guests = [uid(), uid()]
        reduced = self.configured(participants=2)
        results = await asyncio.gather(
            *(reduced.redeem(g, self.room, t) for g, t in zip(guests, tokens)),
            return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, self.RoomError) and r.code == "participant_limit"
                             for r in results), 1)
        loser = next(i for i, r in enumerate(results) if isinstance(r, self.RoomError))
        await self.repo.redeem(guests[loser], self.room, tokens[loser])
        self.assertEqual(len((await self.repo.snapshot(self.owner, self.room))["members"]), 3)

    async def test_revoke_racing_redeem_consistent_winner(self):
        from zvonilka.rooms.repository import invite_digest
        guest = uid()
        token = (await self.repo.issue_invite(self.owner, self.room))["token"]
        revoked, redeemed = await asyncio.gather(
            self.repo.revoke_invite(self.owner, self.room, invite_digest(token)),
            self.repo.redeem(guest, self.room, token), return_exceptions=True)
        members = {m["user_id"] for m in (await self.repo.snapshot(self.owner, self.room))["members"]}
        if isinstance(revoked, dict):
            self.assertIsInstance(redeemed, self.RoomError)
            self.assertEqual(redeemed.code, "invite_revoked")
            self.assertNotIn(guest, members)
        else:
            self.assertIsInstance(revoked, self.RoomError)
            self.assertEqual(revoked.code, "invite_consumed")
            self.assertIsInstance(redeemed, dict)
            self.assertIn(guest, members)

    async def test_consumed_token_retry_survives_original_ttl(self):
        short = self.configured(invite_ttl=1)
        guest = uid()
        token = (await short.issue_invite(self.owner, self.room))["token"]
        await short.redeem(guest, self.room, token)
        await asyncio.sleep(1.15)
        await short.redeem(guest, self.room, token)
        await self.assert_error("invite_consumed", short.redeem(uid(), self.room, token))

    async def test_heartbeat_does_not_extend_room_lifetime(self):
        short = self.configured(lifetime=1)
        owner = uid()
        room = (await short.create(owner))["room_id"]
        generation = uid()
        await short.join(owner, room, generation)
        await short.heartbeat(owner, room, generation)
        await asyncio.sleep(1.15)
        await short.sweep()
        self.assertEqual((await short.snapshot(owner, room))["state"], "closed")
        await self.assert_error("room_closed", short.join(owner, room, uid()))

    async def test_expired_heartbeat_cannot_resurrect_lease(self):
        short = self.configured(presence_ttl=1)
        generation = uid()
        await short.join(self.owner, self.room, generation)
        await asyncio.sleep(1.15)
        await self.assert_error("stale_generation", short.heartbeat(self.owner, self.room, generation))
        snapshot = await short.snapshot(self.owner, self.room)
        self.assertEqual(snapshot["state"], "recovering")
        self.assertFalse(snapshot["members"][0]["online"])
        await short.join(self.owner, self.room, uid())

    async def test_grace_expiry_releases_global_slot(self):
        short = self.configured(rooms=1, grace=1)
        owner = uid()
        room = (await short.create(owner))["room_id"]
        generation = uid()
        await short.join(owner, room, generation)
        await short.leave(owner, room, generation)
        await self.assert_error("room_limit", short.join(self.owner, self.room, uid()))
        await asyncio.sleep(1.15)
        await short.join(self.owner, self.room, uid())
        self.assertEqual((await short.snapshot(owner, room))["state"], "closed")

    async def test_guest_cannot_issue_revoke_or_close(self):
        from zvonilka.rooms.repository import invite_digest
        guest = uid()
        token = (await self.repo.issue_invite(self.owner, self.room))["token"]
        await self.repo.redeem(guest, self.room, token)
        await self.assert_error("not_owner", self.repo.issue_invite(guest, self.room))
        await self.assert_error("not_owner", self.repo.revoke_invite(guest, self.room, invite_digest(token)))
        await self.assert_error("not_owner", self.repo.close(guest, self.room))
        self.assertNotEqual((await self.repo.snapshot(guest, self.room))["state"], "closed")
