import asyncio
import unittest
from collections.abc import Awaitable
from datetime import UTC, datetime
from uuid import uuid4

from zvonilka.rooms.http_errors import STATUSES, room_http_error
from zvonilka.rooms.invitations import decode_invitation, encode_invitation
from zvonilka.rooms.repository import RedisClient, RoomError, await_eval_result, invite_digest
from zvonilka.rooms.service import RoomService


class InvitationTests(unittest.TestCase):
    def setUp(self):
        self.room = uuid4()
        self.token = "A" * 43
        self.text = encode_invitation(self.room, self.token)

    def test_round_trip_and_fixed_length(self):
        value = decode_invitation(self.text)
        self.assertEqual(value.room_id, self.room)
        self.assertEqual(value.token, self.token)
        self.assertEqual(len(self.text), 84)

    def test_repr_is_redacted(self):
        self.assertNotIn(self.token, repr(decode_invitation(self.text)))
        self.assertNotIn(self.text, repr(decode_invitation(self.text)))

    def test_rejects_other_versions_and_urls(self):
        for text in (
            self.text.replace("zv1.", "zv2."),
            "https://host/" + self.text,
            self.text + ".extra",
            " " + self.text,
            self.text + " ",
            "",
            None,
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_invitation(text)

    def test_rejects_invalid_or_noncanonical_uuid(self):
        for value in (
            "not-a-uuid",
            self.room.hex,
            "{" + str(self.room) + "}",
            str(self.room).upper(),
        ):
            if value == str(self.room):
                continue
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_invitation(f"zv1.{value}.{self.token}")

    def test_rejects_invalid_token_without_echoing_it(self):
        for token in ("secret", "A" * 42, "A" * 44, "я" * 43, "!" * 43):
            with self.subTest(length=len(token)):
                with self.assertRaises(ValueError) as ctx:
                    decode_invitation(f"zv1.{self.room}.{token}")
                self.assertNotIn(token, str(ctx.exception))

    def test_encoder_validates_token(self):
        with self.assertRaises(ValueError):
            encode_invitation(self.room, "bad-token")


class ErrorMappingTests(unittest.TestCase):
    def test_unknown_code_not_exposed(self):
        self.assertEqual(room_http_error("password=TOP_SECRET"), (500, "internal_error"))

    def test_known_statuses(self):
        expected = {
            "not_owner": 403,
            "room_not_found": 404,
            "room_closed": 410,
            "invite_consumed": 409,
            "invite_expired": 410,
            "registry_full": 503,
            "schema_mismatch": 503,
        }
        for code, status in expected.items():
            with self.subTest(code=code):
                self.assertEqual(room_http_error(code), (status, code))

    def test_all_mapped_codes_are_errors(self):
        self.assertTrue(all(400 <= status <= 599 for status in STATUSES.values()))


class RecordingRepository:
    """Boundary double only: these tests do not claim to exercise Redis/Lua."""

    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.result: dict[str, object] = {}
        self.error: Exception | None = None

    async def invoke(self, name: str, *args: str) -> dict[str, object]:
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error
        return self.result

    async def create(self, actor: str) -> dict[str, object]:
        return await self.invoke("create", actor)

    async def snapshot(self, actor: str, room_id: str) -> dict[str, object]:
        return await self.invoke("snapshot", actor, room_id)

    async def issue_invite(self, actor: str, room_id: str) -> dict[str, object]:
        return await self.invoke("issue_invite", actor, room_id)

    async def revoke_invite(self, actor: str, room_id: str, digest: str) -> dict[str, object]:
        return await self.invoke("revoke_invite", actor, room_id, digest)

    async def redeem(self, actor: str, room_id: str, token: str) -> dict[str, object]:
        return await self.invoke("redeem", actor, room_id, token)

    async def close(self, actor: str, room_id: str) -> dict[str, object]:
        return await self.invoke("close", actor, room_id)


class RoomServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = RecordingRepository()
        self.service = RoomService(self.repo)
        self.actor, self.room = uuid4(), uuid4()

    async def test_create_binds_actor(self):
        self.repo.result = {"state": "created"}
        self.assertEqual(await self.service.create(self.actor), self.repo.result)
        self.assertEqual(self.repo.calls, [("create", (str(self.actor),))])

    async def test_snapshot_binds_actor_and_room(self):
        await self.service.snapshot(self.actor, self.room)
        self.assertEqual(self.repo.calls, [("snapshot", (str(self.actor), str(self.room)))])

    async def test_issue_returns_copyable_invitation_and_hash_handle(self):
        token = "B" * 43
        self.repo.result = {"token": token, "expires_at": 2000000000}
        issued = await self.service.issue_invite(self.actor, self.room)
        self.assertEqual(issued["invite_id"], invite_digest(token))
        self.assertEqual(decode_invitation(issued["invitation"]).room_id, self.room)
        self.assertEqual(decode_invitation(issued["invitation"]).token, token)
        self.assertEqual(issued["expires_at"], datetime.fromtimestamp(2000000000, UTC))
        self.assertNotIn("token", issued)

    async def test_redeem_extracts_room_and_token(self):
        token = "C" * 43
        await self.service.redeem(self.actor, encode_invitation(self.room, token))
        self.assertEqual(self.repo.calls, [("redeem", (str(self.actor), str(self.room), token))])

    async def test_invalid_invitation_never_reaches_repository(self):
        with self.assertRaises(ValueError):
            await self.service.redeem(self.actor, "wrong")
        self.assertEqual(self.repo.calls, [])

    async def test_close_and_revoke(self):
        await self.service.close(self.actor, self.room)
        await self.service.revoke_invite(self.actor, self.room, "a" * 64)
        self.assertEqual(
            self.repo.calls,
            [
                ("close", (str(self.actor), str(self.room))),
                ("revoke_invite", (str(self.actor), str(self.room), "a" * 64)),
            ],
        )

    async def test_domain_error_not_changed_to_success(self):
        self.repo.error = RoomError("pending_limit")
        with self.assertRaises(RoomError) as ctx:
            await self.service.create(self.actor)
        self.assertEqual(ctx.exception.code, "pending_limit")

    async def test_transport_error_propagates(self):
        self.repo.error = ConnectionError("not for HTTP response")
        with self.assertRaises(ConnectionError):
            await self.service.snapshot(self.actor, self.room)


class RedisEvalContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_coroutine_string(self):
        async def reply() -> str:
            return '{"ok":true}'

        self.assertEqual(await await_eval_result(reply()), '{"ok":true}')

    async def test_native_coroutine_bytes(self):
        async def reply() -> bytes:
            return b'{"ok":true}'

        self.assertEqual(await await_eval_result(reply()), b'{"ok":true}')

    async def test_future_is_awaited(self):
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        future.set_result("future-result")
        self.assertEqual(await await_eval_result(future), "future-result")

    async def test_custom_awaitable_is_awaited(self):
        class Reply:
            def __await__(self):
                async def value() -> str:
                    return "custom-result"

                return value().__await__()

        self.assertEqual(await await_eval_result(Reply()), "custom-result")

    async def test_sync_string_and_bytes_rejected(self):
        for value in ("sync result", b"sync result"):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, "asynchronous"):
                await await_eval_result(value)

    async def test_nonawaitable_objects_rejected(self):
        for value in (None, 1, {}, object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                await await_eval_result(value)

    async def test_wrong_awaited_result_type_rejected(self):
        async def reply() -> int:
            return 123

        with self.assertRaisesRegex(TypeError, "JSON string or bytes"):
            await await_eval_result(reply())

    async def test_exception_propagates_without_text_rewriting(self):
        expected = ConnectionError("unit error")

        async def reply() -> str:
            raise expected

        with self.assertRaises(ConnectionError) as ctx:
            await await_eval_result(reply())
        self.assertIs(ctx.exception, expected)

    async def test_shared_command_union_annotation_is_supported(self):
        class SharedCommand:
            def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Awaitable[str] | str:
                async def reply() -> str:
                    return "shared-command-result"

                return reply()

        client: RedisClient = SharedCommand()
        self.assertEqual(
            await await_eval_result(client.eval("unit-script", 0)),
            "shared-command-result",
        )
