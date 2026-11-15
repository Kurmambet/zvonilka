import asyncio
import json
import unittest
from time import monotonic
from uuid import UUID, uuid4

from signaling_test_support import StubAuth, StubRooms, authentication
from zvonilka.auth.errors import AuthError
from zvonilka.rooms.repository import RoomError
from zvonilka.signaling.service import Policy, Rejected, SignalingService, parse_authentication


class SignalingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(self.auth, self.rooms, presence_ttl=45)
        self.room_id = uuid4()
        self.message = parse_authentication(json.dumps(authentication(self.room_id)))

    async def test_join_uses_wrapped_lua_snapshot_and_verified_identity(self):
        attempt = await self.service.identify(self.message)
        response = await self.service.join(attempt, self.message.request_id)
        self.assertEqual(response.user_id, self.auth.user_id)
        self.assertEqual(response.room.room_id, self.room_id)
        self.assertEqual(response.request_id, self.message.request_id)
        self.assertEqual(response.generation, attempt.generation)
        self.assertEqual(response.capabilities, [])
        self.assertEqual(UUID(self.rooms.joins[0][2]), attempt.generation)
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_bad_auth_does_not_join(self):
        self.auth.error = AuthError("invalid_access")
        with self.assertRaises(AuthError):
            await self.service.identify(self.message)
        self.assertEqual(self.rooms.joins, [])

    async def test_no_leave_before_join_attempt(self):
        attempt = await self.service.identify(self.message)
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, [])

    async def test_error_after_join_attempt_still_allows_generation_cleanup(self):
        attempt = await self.service.identify(self.message)
        self.rooms.error = RoomError("not_admitted")
        with self.assertRaises(RoomError):
            await self.service.join(attempt, self.message.request_id)
        self.assertTrue(attempt.join_attempted)
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_cancelled_join_keeps_cleanup_identity(self):
        self.rooms.join_gate = asyncio.Event()
        attempt = await self.service.identify(self.message)
        task = asyncio.create_task(self.service.join(attempt, self.message.request_id))
        await self.rooms.join_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.service.leave(attempt)
        self.assertEqual(self.rooms.leaves, self.rooms.joins)

    async def test_different_snapshot_room_is_not_success(self):
        self.rooms.bad_snapshot = True
        attempt = await self.service.identify(self.message)
        with self.assertRaises(RuntimeError):
            await self.service.join(attempt, self.message.request_id)

    async def test_generation_is_fresh_for_each_attempt(self):
        first = await self.service.identify(self.message)
        second = await self.service.identify(self.message)
        self.assertNotEqual(first.generation, second.generation)

    async def test_short_session_does_not_outlive_lease_window(self):
        service = SignalingService(self.auth, self.rooms, presence_ttl=2)
        before = monotonic()
        attempt = await service.identify(self.message)
        self.assertEqual(service.session_seconds, 1)
        self.assertGreater(attempt.deadline, before)
        self.assertLessEqual(attempt.deadline - monotonic(), 1)

    async def test_connections_have_a_hard_local_cap(self):
        service = SignalingService(
            self.auth, self.rooms, presence_ttl=45, policy=Policy(max_connections=1)
        )
        self.assertTrue(service.acquire())
        self.assertFalse(service.acquire())
        service.release()
        self.assertTrue(service.acquire())
        service.release()
        self.assertEqual(service.connections, 0)

    async def test_parser_rejects_untrusted_identity(self):
        payload = {**authentication(self.room_id), "user_id": str(uuid4())}
        with self.assertRaises(Rejected) as caught:
            parse_authentication(json.dumps(payload))
        self.assertEqual(caught.exception.code, "invalid_message")

    async def test_parser_classifies_unsupported_version(self):
        for version, code in (
            (2, "unsupported_version"),
            (True, "invalid_message"),
            ("1", "invalid_message"),
            (None, "invalid_message"),
        ):
            with self.subTest(version=version), self.assertRaises(Rejected) as caught:
                parse_authentication(
                    json.dumps({**authentication(self.room_id), "version": version})
                )
            self.assertEqual(caught.exception.code, code)

    async def test_parser_rejects_invalid_json_and_shape(self):
        for raw in ("{", "null", "[]", '"secret"', '{"version": 1}'):
            with self.subTest(raw=raw), self.assertRaises(Rejected) as caught:
                parse_authentication(raw)
            self.assertEqual(caught.exception.code, "invalid_message")

    async def test_policy_rejects_invalid_resource_limits(self):
        with self.assertRaises(ValueError):
            Policy(auth_timeout=0)
        with self.assertRaises(ValueError):
            Policy(session_seconds=float("inf"))
        with self.assertRaises(ValueError):
            Policy(max_connections=True)
        with self.assertRaises(ValueError):
            Policy(max_message_bytes=0)


if __name__ == "__main__":
    unittest.main()
