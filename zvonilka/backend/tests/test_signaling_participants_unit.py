"""Online registry/order tests; no simulation of Redis atomicity."""

import asyncio
import json
import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError
from zvonilka.signaling.participants import ParticipantsHub
from zvonilka.signaling.schemas import ErrorCode, Joined, Participants, server_message_adapter


class Peer:
    def __init__(self, room_id: UUID, user_id: UUID | None = None):
        self.room_id = room_id
        self.user_id = user_id or uuid4()
        self.generation = uuid4()
        self.messages: list[dict] = []
        self.stops: list[ErrorCode] = []
        self.fail = False

    def send(self, message: str) -> None:
        if self.fail:
            raise OSError("secret")
        self.messages.append(json.loads(message))

    def stop(self, reason: ErrorCode) -> None:
        self.stops.append(reason)

    async def admission(self) -> Joined:
        return Joined.model_validate(
            {
                "version": 1,
                "type": "joined",
                "request_id": str(uuid4()),
                "user_id": self.user_id,
                "generation": self.generation,
                "capabilities": [],
                "room": {
                    "room_id": self.room_id,
                    "owner_id": self.user_id,
                    "state": "active",
                    "revision": 999,
                    "members": [{"user_id": self.user_id, "online": True}],
                },
            }
        )


class ParticipantsUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = ParticipantsHub()
        self.room = uuid4()

    async def admit(self, peer: Peer):
        await self.hub.admit(
            peer.room_id,
            peer.user_id,
            peer.generation,
            peer.send,
            peer.stop,
            peer.admission,
        )

    async def test_close_waits_for_admission_then_stops_published_participant(self):
        peer = Peer(self.room)
        started, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        persisted = asyncio.Event()

        async def admission() -> Joined:
            started.set()
            await release.wait()
            return await peer.admission()

        async def persist() -> None:
            persisted.set()

        async def close() -> None:
            closing.set()
            await self.hub.close_room(self.room, persist)

        async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
            tasks.create_task(
                self.hub.admit(
                    self.room, peer.user_id, peer.generation, peer.send, peer.stop, admission
                )
            )
            await started.wait()
            tasks.create_task(close())
            await closing.wait()
            self.assertFalse(persisted.is_set())
            release.set()
        self.assertTrue(persisted.is_set())
        self.assertEqual(peer.stops, ["room_closed"])
        self.assertFalse(self.hub.detach(self.room, peer.user_id, peer.generation))
        self.assertEqual(self.hub.room_count, 0)

    async def test_first_snapshot_follows_own_joined(self):
        peer = Peer(self.room)
        await self.admit(peer)
        self.assertEqual([m["type"] for m in peer.messages], ["joined", "participants"])
        snapshot = Participants.model_validate(peer.messages[-1])
        self.assertEqual(snapshot.sequence, 1)
        self.assertEqual(snapshot.members[0].generation, peer.generation)
        self.assertNotEqual(snapshot.sequence, peer.messages[0]["room"]["revision"])

    async def test_three_peers_receive_full_identical_sorted_snapshot(self):
        peers = [Peer(self.room) for _ in range(3)]
        for peer in peers:
            await self.admit(peer)
        snapshots = [peer.messages[-1] for peer in peers]
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])
        self.assertEqual(snapshots[0]["sequence"], 3)
        self.assertEqual(
            [m["user_id"] for m in snapshots[0]["members"]],
            sorted(str(peer.user_id) for peer in peers),
        )
        for peer in peers:
            self.assertEqual(peer.messages[0]["type"], "joined")

    async def test_disconnect_publishes_remaining_members(self):
        first, second = Peer(self.room), Peer(self.room)
        await self.admit(first)
        await self.admit(second)
        self.assertTrue(self.hub.detach(self.room, second.user_id, second.generation))
        snapshot = Participants.model_validate(first.messages[-1])
        self.assertEqual(snapshot.sequence, 3)
        self.assertEqual([m.user_id for m in snapshot.members], [first.user_id])

    async def test_replacement_and_late_disconnect_preserve_new_generation(self):
        old, observer = Peer(self.room), Peer(self.room)
        await self.admit(old)
        await self.admit(observer)
        replacement = Peer(self.room, old.user_id)
        await self.admit(replacement)
        before = observer.messages[-1]
        self.assertFalse(self.hub.detach(self.room, old.user_id, old.generation))
        self.assertEqual(observer.messages[-1], before)
        members = Participants.model_validate(before).members
        self.assertEqual(len(members), 2)
        self.assertEqual(
            next(m.generation for m in members if m.user_id == old.user_id), replacement.generation
        )

    async def test_new_epoch_after_last_disconnect(self):
        first = Peer(self.room)
        await self.admit(first)
        old_epoch = first.messages[-1]["epoch"]
        self.hub.detach(self.room, first.user_id, first.generation)
        self.assertEqual(self.hub.room_count, 0)
        second = Peer(self.room)
        await self.admit(second)
        self.assertNotEqual(second.messages[-1]["epoch"], old_epoch)
        self.assertEqual(second.messages[-1]["sequence"], 1)

    async def test_failed_admission_does_not_publish_or_leave_empty_room(self):
        peer = Peer(self.room)

        async def fail() -> Joined:
            raise RuntimeError("denied")

        with self.assertRaises(RuntimeError):
            await self.hub.admit(
                self.room, peer.user_id, peer.generation, peer.send, peer.stop, fail
            )
        self.assertEqual(peer.messages, [])
        self.assertEqual(self.hub.room_count, 0)

    async def test_overlapping_admissions_publish_in_serial_order(self):
        first, second = Peer(self.room), Peer(self.room)
        started, gate = asyncio.Event(), asyncio.Event()

        async def delayed() -> Joined:
            started.set()
            await gate.wait()
            return await first.admission()

        task = asyncio.create_task(
            self.hub.admit(
                self.room, first.user_id, first.generation, first.send, first.stop, delayed
            )
        )
        await started.wait()
        other = asyncio.create_task(self.admit(second))
        await asyncio.sleep(0)
        self.assertEqual(second.messages, [])
        gate.set()
        await asyncio.wait_for(asyncio.gather(task, other), 1)
        self.assertEqual(
            [m["sequence"] for m in first.messages if m["type"] == "participants"], [1, 2]
        )
        self.assertEqual(second.messages[-1]["sequence"], 2)

    async def test_detach_while_join_is_awaiting_preserves_epoch_and_order(self):
        first, second = Peer(self.room), Peer(self.room)
        await self.admit(first)
        epoch = first.messages[-1]["epoch"]
        started, gate = asyncio.Event(), asyncio.Event()

        async def delayed() -> Joined:
            started.set()
            await gate.wait()
            return await second.admission()

        task = asyncio.create_task(
            self.hub.admit(
                self.room, second.user_id, second.generation, second.send, second.stop, delayed
            )
        )
        await started.wait()
        self.hub.detach(self.room, first.user_id, first.generation)
        self.assertEqual(self.hub.room_count, 1)
        gate.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(second.messages[-1]["epoch"], epoch)
        self.assertEqual(second.messages[-1]["sequence"], 3)
        self.hub.detach(self.room, second.user_id, second.generation)
        self.assertEqual(self.hub.room_count, 0)

    async def test_cancelled_join_drops_unused_room(self):
        peer = Peer(self.room)
        started = asyncio.Event()

        async def blocked() -> Joined:
            started.set()
            await asyncio.Event().wait()
            return await peer.admission()

        task = asyncio.create_task(
            self.hub.admit(self.room, peer.user_id, peer.generation, peer.send, peer.stop, blocked)
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.hub.room_count, 0)

    async def test_failed_subscriber_does_not_block_other_recipient(self):
        first, second = Peer(self.room), Peer(self.room)
        await self.admit(first)
        first.fail = True
        await self.admit(second)
        self.assertEqual(second.messages[-1]["type"], "participants")
        self.assertEqual(len(second.messages[-1]["members"]), 2)
        self.assertEqual(first.stops, ["overloaded"])
        self.hub.detach(self.room, first.user_id, first.generation)
        self.assertEqual(len(second.messages[-1]["members"]), 1)

    async def test_other_rooms_do_not_share_epochs_or_sequences(self):
        first, second = Peer(self.room), Peer(uuid4())
        await self.admit(first)
        await self.admit(second)
        self.assertNotEqual(first.messages[-1]["epoch"], second.messages[-1]["epoch"])
        self.assertEqual(second.messages[-1]["sequence"], 1)
        self.assertEqual(len(first.messages), 2)


class ParticipantsSchemaTests(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "version": 1,
            "type": "participants",
            "room_id": str(uuid4()),
            "epoch": str(uuid4()),
            "sequence": 1,
            "members": [{"user_id": str(uuid4()), "generation": str(uuid4())}],
        }

    def test_round_trip_through_server_adapter(self):
        message = Participants.model_validate(self.payload())
        self.assertEqual(server_message_adapter.validate_json(message.model_dump_json()), message)

    def test_duplicate_ids_and_generations_rejected(self):
        for field in ("user_id", "generation"):
            payload = self.payload()
            payload["members"].append({"user_id": str(uuid4()), "generation": str(uuid4())})
            payload["members"][1][field] = payload["members"][0][field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Participants.model_validate(payload)

    def test_sequence_types_and_participant_bound(self):
        for value in (0, -1, True, 1.0, "1"):
            with self.subTest(sequence=value), self.assertRaises(ValidationError):
                Participants.model_validate({**self.payload(), "sequence": value})
        payload = self.payload()
        payload["members"] = [
            {"user_id": str(uuid4()), "generation": str(uuid4())} for _ in range(4)
        ]
        with self.assertRaises(ValidationError):
            Participants.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
