"""In-memory dependency doubles, not Redis/PostgreSQL or atomicity simulations."""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from zvonilka.auth.errors import AuthError
from zvonilka.rooms.repository import RoomError


@dataclass(frozen=True)
class TestIdentity:
    user_id: UUID


class StubAuth:
    def __init__(self):
        self.user_id = uuid4()
        self.calls = 0
        self.error: Exception | None = None

    async def authenticate(self, access_token: str) -> TestIdentity:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if access_token != "test-access-token":
            raise AuthError("invalid_access")
        return TestIdentity(self.user_id)


class StubRooms:
    def __init__(self):
        self.joins: list[tuple[str, str, str]] = []
        self.leaves: list[tuple[str, str, str]] = []
        self.error: Exception | None = None
        self.leave_error: Exception | None = None
        self.join_started = asyncio.Event()
        self.join_gate: asyncio.Event | None = None
        self.bad_snapshot = False
        self.present: dict[tuple[str, str], str] = {}
        self.heartbeats: list[tuple[str, str, str]] = []
        self.heartbeat_error: Exception | None = None
        self.heartbeat_gate: asyncio.Event | None = None
        self.heartbeat_started = asyncio.Event()

    async def join(self, actor: str, room_id: str, generation: str) -> dict:
        self.joins.append((actor, room_id, generation))
        self.join_started.set()
        if self.join_gate is not None:
            await self.join_gate.wait()
        if self.error is not None:
            raise self.error
        previous = self.present.get((actor, room_id))
        self.present[(actor, room_id)] = generation
        result: dict = {
            "room": {
                "room_id": str(uuid4()) if self.bad_snapshot else room_id,
                "owner_id": actor,
                "state": "active",
                "revision": 2,
                "members": [{"user_id": actor, "online": True}],
                "activated_at": 1800000000,
                "deadline": 1800043200,
            }
        }

        if previous is not None and previous != generation:
            result["replaced_generation"] = previous
        return result

    async def heartbeat(self, actor: str, room_id: str, generation: str) -> dict:
        self.heartbeats.append((actor, room_id, generation))
        self.heartbeat_started.set()
        if self.heartbeat_gate is not None:
            await self.heartbeat_gate.wait()
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        if self.present.get((actor, room_id)) != generation:
            raise RoomError("stale_generation")
        return {}

    async def leave(self, actor: str, room_id: str, generation: str) -> dict:
        self.leaves.append((actor, room_id, generation))
        if self.leave_error is not None:
            raise self.leave_error
        if self.present.get((actor, room_id)) != generation:
            raise RoomError("stale_generation")
        del self.present[(actor, room_id)]
        return {}


def authentication(room_id: UUID) -> dict:
    return {
        "version": 1,
        "type": "authenticate",
        "request_id": str(uuid4()),
        "room_id": str(room_id),
        "access_token": "test-access-token",
        "app_version": "0.5.0-dev.1",
        "capabilities": [],
    }
