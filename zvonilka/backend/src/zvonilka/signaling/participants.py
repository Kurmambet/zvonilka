"""Single-process online snapshots; admission and leases still belong to Redis."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .schemas import ErrorCode, Joined, OnlinePeer, Participants


class Subscriber:
    def __init__(
        self, generation: UUID, send: Callable[[str], None], stop: Callable[[ErrorCode], None]
    ):
        self.generation = generation
        self.send = send
        self.stop = stop


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
                OnlinePeer(user_id=user_id, generation=members[user_id].generation)
                for user_id in sorted(members, key=str)
            ],
        )
        payload = snapshot.model_dump_json()
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
                members[user_id] = Subscriber(generation, send, stop)
                self._commit(room_id, room, members)
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
