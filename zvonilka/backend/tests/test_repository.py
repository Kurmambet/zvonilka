import asyncio
import hashlib
import json
import unittest
from uuid import uuid4

from zvonilka.rooms.repository import (
    Limits, RoomError, RoomRepository, canonical_id, invite_digest, validate_digest,
)


def uid():
    return str(uuid4())


class RecordingRedis:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"ok": True, "value": {}}

    async def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        return json.dumps(self.response)


class ValidationTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(Limits().rooms, 2)
        self.assertEqual(Limits().participants, 3)

    def test_invalid_limits(self):
        for kwargs in ({"rooms": 0}, {"rooms": True}, {"rooms": 1.5},
                       {"grace": -1}, {"participants": 4},
                       {"max_records": 129}, {"invites_per_room": 33}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Limits(**kwargs)

    def test_digest(self):
        token = "A" * 43
        self.assertEqual(invite_digest(token), hashlib.sha256(token.encode()).hexdigest())

    def test_invalid_tokens(self):
        for token in ("", "x" * 42, "x" * 44, " " * 43, "я" * 43, None):
            with self.subTest(token=token), self.assertRaises(ValueError):
                invite_digest(token)

    def test_digest_validation(self):
        self.assertEqual(validate_digest("a" * 64), "a" * 64)
        with self.assertRaises(ValueError):
            validate_digest("x" * 64)

    def test_uuid_normalization(self):
        value = uid()
        self.assertEqual(canonical_id(value.upper()), value)

    def test_bad_key(self):
        with self.assertRaises(ValueError):
            RoomRepository(RecordingRedis(), key="bad key")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = RecordingRedis()
        self.repo = RoomRepository(self.redis)
        self.actor, self.room, self.generation = uid(), uid(), uid()

    def request(self):
        return json.loads(self.redis.calls[-1][2][1])

    async def test_create(self):
        await self.repo.create(self.actor, room_id=self.room)
        self.assertEqual(self.request()["op"], "create")
        self.assertEqual(self.request()["actor"], self.actor)
        self.assertEqual(self.redis.calls[-1][1], 1)

    async def test_invite_secret_not_sent_to_redis(self):
        result = await self.repo.issue_invite(self.actor, self.room)
        self.assertEqual(len(result["token"]), 43)
        self.assertNotIn(result["token"], self.redis.calls[-1][2][1])
        self.assertEqual(self.request()["digest"], invite_digest(result["token"]))

    async def test_redeem_hashes_secret(self):
        token = "B" * 43
        await self.repo.redeem(self.actor, self.room, token)
        self.assertEqual(self.request()["digest"], invite_digest(token))
        self.assertNotIn(token, self.redis.calls[-1][2][1])

    async def test_domain_error(self):
        self.redis.response = {"ok": False, "code": "room_limit"}
        with self.assertRaises(RoomError) as caught:
            await self.repo.create(self.actor)
        self.assertEqual(caught.exception.code, "room_limit")

    async def test_bad_uuid_does_not_call_redis(self):
        with self.assertRaises(ValueError):
            await self.repo.create("not-a-uuid")
        self.assertEqual(self.redis.calls, [])

    async def test_bad_token_does_not_call_redis(self):
        with self.assertRaises(ValueError):
            await self.repo.redeem(self.actor, self.room, "bad")
        self.assertEqual(self.redis.calls, [])

    async def test_join(self):
        await self.repo.join(self.actor, self.room, self.generation, overloaded=True)
        self.assertTrue(self.request()["overloaded"])
        self.assertEqual(self.request()["generation"], self.generation)

    async def test_strict_overload(self):
        with self.assertRaises(ValueError):
            await self.repo.join(self.actor, self.room, self.generation, overloaded="false")

    async def test_transport_error_propagates(self):
        class BrokenRedis:
            async def eval(self, *args):
                raise ConnectionError("unavailable")
        with self.assertRaises(ConnectionError):
            await RoomRepository(BrokenRedis()).create(self.actor)

    async def test_operations(self):
        for name, args in (
            ("snapshot", (self.actor, self.room)),
            ("heartbeat", (self.actor, self.room, self.generation)),
            ("leave", (self.actor, self.room, self.generation)),
            ("close", (self.actor, self.room)),
            ("revoke_invite", (self.actor, self.room, "c" * 64)),
            ("sweep", ()),
        ):
            with self.subTest(name=name):
                await getattr(self.repo, name)(*args)
                self.assertEqual(self.request()["op"], name)


if __name__ == "__main__":
    unittest.main()
