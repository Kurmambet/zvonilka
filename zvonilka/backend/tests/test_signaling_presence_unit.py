"""Presence service concurrency/ownership tests; no claim of Redis atomicity."""

import asyncio
import json
import unittest
from time import monotonic
from uuid import uuid4

from pydantic import ValidationError
from signaling_test_support import StubAuth, StubRooms, authentication
from zvonilka.rooms.repository import RoomError
from zvonilka.signaling.schemas import (
    Heartbeat,
    HeartbeatAck,
    client_message_adapter,
    server_message_adapter,
)
from zvonilka.signaling.service import Policy, Rejected, SignalingService, parse_authentication


class PresenceUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = StubAuth()
        self.rooms = StubRooms()
        self.service = SignalingService(self.auth, self.rooms, presence_ttl=45)
        self.message = parse_authentication(json.dumps(authentication(uuid4())))

    async def joined(self):
        attempt = await self.service.identify(self.message)
        await self.service.join(attempt, self.message.request_id)
        attempt.next_heartbeat_at = 0
        return attempt

    def heartbeat(self, generation):
        return Heartbeat(version=1, type="heartbeat", request_id=uuid4(), generation=generation)

    async def test_ack_correlates_and_renews_only_after_success(self):
        attempt = await self.joined()
        previous = attempt.deadline
        message = self.heartbeat(attempt.generation)
        ack = await self.service.heartbeat(attempt, message)
        self.assertEqual(ack.request_id, message.request_id)
        self.assertEqual(ack.generation, attempt.generation)
        self.assertGreaterEqual(attempt.deadline, previous)
        self.assertEqual(self.rooms.heartbeats[-1][2], str(attempt.generation))
        await self.service.leave(attempt)
        self.assertEqual(self.service.registry_size, 0)

    async def test_wrong_generation_never_reaches_repository(self):
        attempt = await self.joined()
        with self.assertRaises(Rejected) as caught:
            await self.service.heartbeat(attempt, self.heartbeat(uuid4()))
        self.assertEqual(caught.exception.code, "stale_generation")
        self.assertEqual(self.rooms.heartbeats, [])

    async def test_replacement_stops_old_and_blocks_its_heartbeat(self):
        first = await self.joined()
        second = await self.joined()
        self.assertTrue(first.connection.stopped.is_set())
        self.assertEqual(first.connection.reason, "connection_replaced")
        with self.assertRaises(Rejected):
            await self.service.heartbeat(first, self.heartbeat(first.generation))
        self.assertEqual(self.rooms.heartbeats, [])
        await self.service.heartbeat(second, self.heartbeat(second.generation))

    async def test_late_leave_cannot_forget_or_remove_new_generation(self):
        first = await self.joined()
        second = await self.joined()
        with self.assertRaises(RoomError):
            await self.service.leave(first)
        self.assertEqual(self.service.registry_size, 1)
        self.assertEqual(
            self.rooms.present[(str(second.user_id), str(second.room_id))], str(second.generation)
        )
        await self.service.heartbeat(second, self.heartbeat(second.generation))
        await self.service.leave(second)
        self.assertEqual(self.service.registry_size, 0)

    async def test_overlapping_joins_have_one_local_winner(self):
        self.rooms.join_gate = asyncio.Event()
        first = await self.service.identify(self.message)
        second = await self.service.identify(self.message)
        first_task = asyncio.create_task(self.service.join(first, self.message.request_id))
        await self.rooms.join_started.wait()
        second_task = asyncio.create_task(self.service.join(second, self.message.request_id))
        await asyncio.sleep(0)
        self.assertEqual(len(self.rooms.joins), 1)
        self.rooms.join_gate.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)
        self.assertEqual(len(self.rooms.joins), 2)
        self.assertTrue(first.connection.stopped.is_set())
        self.assertFalse(second.connection.stopped.is_set())
        self.assertEqual(self.service.registry_size, 1)
        await self.service.leave(second)
        self.assertEqual(self.service.registry_size, 0)

    async def test_cancelled_waiter_does_not_destroy_shared_gate(self):
        self.rooms.join_gate = asyncio.Event()
        first = await self.service.identify(self.message)
        second = await self.service.identify(self.message)
        first_task = asyncio.create_task(self.service.join(first, self.message.request_id))
        await self.rooms.join_started.wait()
        second_task = asyncio.create_task(self.service.join(second, self.message.request_id))
        await asyncio.sleep(0)
        second_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second_task
        self.rooms.join_gate.set()
        await asyncio.wait_for(first_task, timeout=2)
        self.assertEqual(len(self.rooms.joins), 1)
        await self.service.leave(first)
        self.assertEqual(self.service.registry_size, 0)

    async def test_heartbeat_serializes_with_replacement(self):
        first = await self.joined()
        self.rooms.heartbeat_gate = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self.service.heartbeat(first, self.heartbeat(first.generation))
        )
        await self.rooms.heartbeat_started.wait()
        second = await self.service.identify(self.message)
        join_task = asyncio.create_task(self.service.join(second, self.message.request_id))
        await asyncio.sleep(0)
        self.assertEqual(len(self.rooms.joins), 1)
        self.rooms.heartbeat_gate.set()
        await asyncio.wait_for(asyncio.gather(heartbeat_task, join_task), timeout=2)
        self.assertTrue(first.connection.stopped.is_set())
        with self.assertRaises(Rejected):
            await self.service.heartbeat(first, self.heartbeat(first.generation))
        await self.service.leave(second)

    async def test_rate_limit_does_not_renew_presence(self):
        attempt = await self.service.identify(self.message)
        await self.service.join(attempt, self.message.request_id)
        with self.assertRaises(Rejected) as caught:
            await self.service.heartbeat(attempt, self.heartbeat(attempt.generation))
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(self.rooms.heartbeats, [])

    async def test_repository_failure_does_not_advance_deadline(self):
        attempt = await self.joined()
        previous = attempt.deadline
        self.rooms.heartbeat_error = ConnectionError("secret")
        with self.assertRaises(ConnectionError):
            await self.service.heartbeat(attempt, self.heartbeat(attempt.generation))
        self.assertEqual(attempt.deadline, previous)

    async def test_expired_local_window_is_not_resurrected(self):
        attempt = await self.joined()
        attempt.deadline = monotonic() - 1
        with self.assertRaises(Rejected) as caught:
            await self.service.heartbeat(attempt, self.heartbeat(attempt.generation))
        self.assertEqual(caught.exception.code, "heartbeat_timeout")
        self.assertEqual(self.rooms.heartbeats, [])

    async def test_stopped_attempt_cannot_join(self):
        attempt = await self.service.identify(self.message)
        attempt.connection.stop("server_shutdown")
        with self.assertRaises(Rejected):
            await self.service.join(attempt, self.message.request_id)
        self.assertEqual(self.rooms.joins, [])
        self.assertEqual(self.service.registry_size, 0)

    async def test_failed_replacement_does_not_stop_existing_local_owner(self):
        first = await self.joined()
        self.rooms.error = RoomError("room_limit")
        second = await self.service.identify(self.message)
        with self.assertRaises(RoomError):
            await self.service.join(second, self.message.request_id)
        self.assertFalse(first.connection.stopped.is_set())
        await self.service.heartbeat(first, self.heartbeat(first.generation))

    async def test_shutdown_stops_and_waits_for_registered_handlers(self):
        self.assertTrue(self.service.acquire())
        connection = self.service.track()

        async def handler():
            await connection.stopped.wait()
            self.service.release(connection)

        task = asyncio.create_task(handler())
        await self.service.shutdown()
        await task
        self.assertEqual(connection.reason, "server_shutdown")
        self.assertEqual(self.service.connections, 0)
        self.assertFalse(self.service.acquire())
        await self.service.shutdown()

    async def test_shutdown_timeout_is_explicit_and_does_not_leak_wait_tasks(self):
        service = SignalingService(
            self.auth, self.rooms, presence_ttl=45, policy=Policy(shutdown_timeout=0.02)
        )
        service.acquire()
        connection = service.track()
        baseline = set(asyncio.all_tasks())
        with self.assertRaises(TimeoutError):
            await service.shutdown()
        await asyncio.sleep(0)
        self.assertEqual(set(asyncio.all_tasks()), baseline)
        self.assertTrue(service.closing)
        service.release(connection)

    async def test_registry_is_bounded_by_live_work_not_historical_users(self):
        for _ in range(50):
            self.auth.user_id = uuid4()
            attempt = await self.joined()
            await self.service.leave(attempt)
            self.assertEqual(self.service.registry_size, 0)
        self.assertEqual(self.rooms.present, {})

    async def test_leave_failure_still_forgets_local_owner(self):
        attempt = await self.joined()
        self.rooms.leave_error = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await self.service.leave(attempt)
        self.assertEqual(self.service.registry_size, 0)


class PresenceSchemaTests(unittest.TestCase):
    def test_heartbeat_and_ack_round_trip(self):
        message = Heartbeat(version=1, type="heartbeat", request_id=uuid4(), generation=uuid4())
        parsed = client_message_adapter.validate_json(message.model_dump_json())
        self.assertEqual(parsed, message)
        ack = HeartbeatAck(
            version=1,
            type="heartbeat_ack",
            request_id=message.request_id,
            generation=message.generation,
        )
        self.assertEqual(HeartbeatAck.model_validate_json(ack.model_dump_json()), ack)
        self.assertEqual(server_message_adapter.validate_json(ack.model_dump_json()), ack)

    def test_untrusted_heartbeat_fields_are_rejected(self):
        payload = {
            "version": 1,
            "type": "heartbeat",
            "request_id": str(uuid4()),
            "generation": str(uuid4()),
        }
        for name in ("user_id", "room_id", "access_token"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                Heartbeat.model_validate({**payload, name: "untrusted"})
        for name in ("request_id", "generation"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                Heartbeat.model_validate({**payload, name: "invalid"})
            with self.subTest(missing=name), self.assertRaises(ValidationError):
                Heartbeat.model_validate(
                    {key: value for key, value in payload.items() if key != name}
                )


if __name__ == "__main__":
    unittest.main()
