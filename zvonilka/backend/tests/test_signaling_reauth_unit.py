"""Service-level reauthentication rules; auth and Redis are explicit dependency doubles."""

import asyncio
import contextlib
import json
import unittest
from uuid import UUID, uuid4

from signaling_test_support import StubAuth, StubRooms, TestAccess, authentication
from zvonilka.auth.errors import AuthError
from zvonilka.rooms.repository import RoomError
from zvonilka.signaling.auth_protocol import Reauthenticate
from zvonilka.signaling.service import (
    Attempt,
    Policy,
    Rejected,
    SignalingService,
    parse_active,
    parse_authentication,
)


class GatedAuth(StubAuth):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.hold = False

    async def authenticate_access(self, access_token: str) -> TestAccess:
        if self.hold:
            self.started.set()
            await self.gate.wait()
        return await super().authenticate_access(access_token)


class ReauthenticateUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth, self.rooms = GatedAuth(), StubRooms()
        self.service = SignalingService(
            self.auth, self.rooms, presence_ttl=45, policy=Policy(reauth_min_interval=60)
        )
        self.room_id = uuid4()
        self.message = parse_authentication(json.dumps(authentication(self.room_id)))
        self.attempts: list[Attempt] = []
        self.attempt = await self.connect()
        self.addAsyncCleanup(self.cleanup)

    async def connect(self) -> Attempt:
        attempt = await self.service.identify(self.message)
        await self.service.join(attempt, self.message.request_id)
        self.attempts.append(attempt)
        return attempt

    async def cleanup(self):
        for attempt in self.attempts:
            with contextlib.suppress(RoomError):
                await self.service.leave(attempt)
        self.assertEqual(self.service.registry_size, 0)

    def request(self, token: str = "next-token", generation: UUID | None = None) -> Reauthenticate:
        return Reauthenticate.model_validate(
            {
                "version": 1,
                "type": "reauthenticate",
                "request_id": uuid4(),
                "generation": generation or self.attempt.generation,
                "access_token": token,
            }
        )

    def allow_next(self, attempt: Attempt | None = None) -> None:
        (attempt or self.attempt).next_reauth_at = 0.0

    async def assert_rejected(self, code: str, operation) -> None:
        with self.assertRaises(Rejected) as caught:
            await operation
        self.assertEqual(caught.exception.code, code)

    async def test_identify_binds_verified_session_and_expiry(self):
        window = self.attempt.authorization
        self.assertEqual(window.user_id, self.auth.user_id)
        self.assertEqual(window.session_id, self.auth.session_id)
        self.assertEqual(window.expires_at, self.auth.expires_at)

    async def test_success_keeps_generation_lease_and_redis_untouched(self):
        expires_at = self.auth.expires_at + 120
        self.auth.grant("next-token", expires_at=expires_at)
        before = (
            self.attempt.generation,
            self.attempt.deadline,
            list(self.rooms.joins),
            list(self.rooms.heartbeats),
            list(self.rooms.leaves),
        )
        request = self.request()
        response = await self.service.reauthenticate(self.attempt, request)
        self.assertEqual(response.request_id, request.request_id)
        self.assertEqual(response.generation, self.attempt.generation)
        self.assertEqual(response.expires_at, expires_at)
        self.assertEqual(self.attempt.authorization.expires_at, expires_at)
        after = (
            self.attempt.generation,
            self.attempt.deadline,
            list(self.rooms.joins),
            list(self.rooms.heartbeats),
            list(self.rooms.leaves),
        )
        self.assertEqual(after, before)

    async def test_other_generation_never_reaches_authentication(self):
        calls = self.auth.calls
        await self.assert_rejected(
            "stale_generation",
            self.service.reauthenticate(self.attempt, self.request(generation=uuid4())),
        )
        self.assertEqual(self.auth.calls, calls)

    async def test_other_account_or_session_is_rejected(self):
        for name in ("user_id", "session_id"):
            with self.subTest(changed=name):
                if name == "user_id":
                    self.auth.grant("next-token", user_id=uuid4())
                else:
                    self.auth.grant("next-token", session_id=uuid4())
                self.allow_next()
                await self.assert_rejected(
                    "invalid_access", self.service.reauthenticate(self.attempt, self.request())
                )
                self.assertEqual(self.attempt.authorization.expires_at, self.auth.expires_at)

    async def test_older_expiry_does_not_replace_current_authorization(self):
        self.auth.grant("next-token", expires_at=self.auth.expires_at - 1)
        await self.assert_rejected(
            "invalid_access", self.service.reauthenticate(self.attempt, self.request())
        )
        self.assertEqual(self.attempt.authorization.expires_at, self.auth.expires_at)

    async def test_authentication_error_leaves_window_unchanged(self):
        with self.assertRaises(AuthError):
            await self.service.reauthenticate(self.attempt, self.request("unknown"))
        self.assertEqual(self.attempt.authorization.expires_at, self.auth.expires_at)

    async def test_rate_limit_is_checked_before_database_and_counts_failures(self):
        with self.assertRaises(AuthError):
            await self.service.reauthenticate(self.attempt, self.request("unknown"))
        self.auth.grant("next-token")
        calls = self.auth.calls
        await self.assert_rejected(
            "rate_limited", self.service.reauthenticate(self.attempt, self.request())
        )
        self.assertEqual(self.auth.calls, calls)

    async def test_unjoined_attempt_cannot_reauthenticate(self):
        attempt = await self.service.identify(self.message)
        self.addAsyncCleanup(self.service.leave, attempt)
        self.auth.grant("next-token")
        calls = self.auth.calls
        await self.assert_rejected(
            "stale_generation",
            self.service.reauthenticate(attempt, self.request(generation=attempt.generation)),
        )
        self.assertEqual(self.auth.calls, calls)

    async def test_replacement_during_database_check_is_not_renewed(self):
        self.auth.grant("next-token", expires_at=self.auth.expires_at + 120)
        self.auth.hold = True
        task = asyncio.create_task(self.service.reauthenticate(self.attempt, self.request()))
        await asyncio.wait_for(self.auth.started.wait(), 1)
        self.auth.hold = False
        await self.connect()
        self.auth.gate.set()
        await self.assert_rejected("connection_replaced", task)
        self.assertEqual(self.attempt.authorization.expires_at, self.auth.expires_at)

    async def test_shutdown_rejects_without_database_call(self):
        calls = self.auth.calls
        self.service.closing = True
        try:
            await self.assert_rejected(
                "server_shutdown", self.service.reauthenticate(self.attempt, self.request())
            )
        finally:
            self.service.closing = False
        self.assertEqual(self.auth.calls, calls)

    async def test_parse_accepts_only_declared_reauthentication_shape(self):
        data = {
            "version": 1,
            "type": "reauthenticate",
            "request_id": str(uuid4()),
            "generation": str(uuid4()),
            "access_token": "private-token",
        }
        parsed = parse_active(json.dumps(data))
        self.assertIsInstance(parsed, Reauthenticate)
        self.assertNotIn("private-token", repr(parsed))
        for extra in ("user_id", "session_id", "room_id", "expires_at", "refresh_token"):
            with self.subTest(extra=extra), self.assertRaises(Rejected) as caught:
                parse_active(json.dumps({**data, extra: str(uuid4())}))
            self.assertEqual(caught.exception.code, "invalid_message")

    async def test_policy_rejects_invalid_interval(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Policy(reauth_min_interval=value)
