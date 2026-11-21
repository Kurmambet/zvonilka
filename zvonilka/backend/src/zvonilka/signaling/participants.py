"""Single-process online snapshots; admission and leases still belong to Redis."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

from .schemas import ErrorCode, Joined, OnlinePeer, Participants

Delivery = Literal[
    "sent", "sender_stale", "invalid_target", "peer_unavailable", "peer_replaced", "peer_overloaded"
]


class Subscriber:
    def __init__(
        self,
        generation: UUID,
        send: Callable[[str], None],
        stop: Callable[[ErrorCode], None],
        ready: Callable[[], bool] | None = None,
        display_name: str | None = None,
    ):
        self.generation = generation
        self.send = send
        self.stop = stop
        self.ready = ready
        self.display_name = display_name


@dataclass(slots=True)
class Room:
    # Local room lifecycle; never compare this counter with Redis room.revision.
    epoch: UUID = field(default_factory=uuid4)
    sequence: int = 0
    members: dict[UUID, Subscriber] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ParticipantsHub:
    def __init__(self):
        self._rooms: dict[UUID, Room] = {}

    @property
    def room_count(self) -> int:
        return len(self._rooms)

    def _discard_empty(self, room_id: UUID, room: Room) -> None:
        # Pending joins keep the epoch alive even if the last online peer leaves.
        if room.users == 0 and not room.members:
            self._rooms.pop(room_id, None)

    def _commit(self, room_id: UUID, room: Room, members: dict[UUID, Subscriber]) -> None:
        # Mesh v1 caps rooms at three: keep full snapshots until that cap changes.
        snapshot = Participants(
            version=1,
            type="participants",
            room_id=room_id,
            epoch=room.epoch,
            sequence=room.sequence + 1,
            members=[
                OnlinePeer(
                    user_id=user_id,
                    generation=members[user_id].generation,
                    display_name=members[user_id].display_name,
                )
                for user_id in sorted(members, key=str)
            ],
        )
        payload = snapshot.model_dump_json(exclude_none=True)
        room.members = members
        room.sequence = snapshot.sequence
        for member in members.values():
            try:
                member.send(payload)
            except Exception:  # noqa: BLE001 -- One failed subscriber must not block the others.
                member.stop("overloaded")

    async def admit(
        self,
        room_id: UUID,
        user_id: UUID,
        generation: UUID,
        send: Callable[[str], None],
        stop: Callable[[ErrorCode], None],
        admission: Callable[[], Awaitable[Joined]],
        ready: Callable[[], bool] | None = None,
        *,
        display_name: str | None = None,
    ) -> None:
        room = self._rooms.setdefault(room_id, Room())
        room.users += 1
        try:
            async with room.lock:
                joined = await admission()
                if (
                    joined.room.room_id != room_id
                    or joined.user_id != user_id
                    or joined.generation != generation
                ):
                    raise RuntimeError("admission identity mismatch")
                # No await here: FIFO gives this socket joined before its first snapshot.
                send(joined.model_dump_json())
                members = dict(room.members)
                members[user_id] = Subscriber(generation, send, stop, ready, display_name)
                self._commit(room_id, room, members)
        finally:
            room.users -= 1
            self._discard_empty(room_id, room)

    async def close_room(self, room_id: UUID, persist: Callable[[], Awaitable[None]]) -> None:
        room = self._rooms.setdefault(room_id, Room())
        room.users += 1
        try:
            # Share the admission lock: no committed join can publish after this close.
            async with room.lock:
                await persist()
                # Remove routing before waking handlers. Redis owns the closed state.
                members, room.members = room.members, {}
                for member in members.values():
                    member.stop("room_closed")
        finally:
            room.users -= 1
            self._discard_empty(room_id, room)

    def detach(self, room_id: UUID, user_id: UUID, generation: UUID) -> bool:
        # Pure synchronous changes may run while another join awaits Redis.
        room = self._rooms.get(room_id)
        if room is None:
            return False
        member = room.members.get(user_id)
        if member is None or member.generation != generation:
            return False
        members = dict(room.members)
        del members[user_id]
        self._commit(room_id, room, members)
        self._discard_empty(room_id, room)
        return True

    def deliver(
        self,
        room_id: UUID,
        sender_id: UUID,
        sender_generation: UUID,
        target_id: UUID,
        target_generation: UUID,
        payload: str,
    ) -> Delivery:
        # Lookup, generation checks and enqueue contain no await: one-process fence.
        room = self._rooms.get(room_id)
        sender = room.members.get(sender_id) if room is not None else None
        if sender is None or sender.generation != sender_generation:
            return "sender_stale"
        if sender.ready is not None and not sender.ready():
            return "sender_stale"
        if sender_id == target_id:
            return "invalid_target"
        target = room.members.get(target_id) if room is not None else None
        if target is None:
            return "peer_unavailable"
        if target.generation != target_generation:
            return "peer_replaced"
        if target.ready is not None and not target.ready():
            return "peer_unavailable"
        try:
            target.send(payload)
        except Exception:  # noqa: BLE001 -- Do not propagate another socket's failure.
            target.stop("overloaded")
            return "peer_overloaded"
        return "sent"
