from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Any, Protocol
from uuid import UUID, uuid4


class RedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: str) -> Any: ...


class RoomError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Limits:
    rooms: int = 2
    owner_rooms: int = 1
    participants: int = 3
    pending_per_owner: int = 3
    pending_ttl: int = 86400
    grace: int = 120
    lifetime: int = 43200
    invite_ttl: int = 86400
    presence_ttl: int = 45
    closed_retention: int = 86400
    max_records: int = 128
    invites_per_room: int = 32

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.participants > 3:
            raise ValueError("mesh v1 supports at most three participants")
        if self.max_records > 128 or self.invites_per_room > 32:
            raise ValueError("aggregate safety bounds exceeded")


def canonical_id(value: str) -> str:
    return str(UUID(value))


def invite_digest(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("invalid invitation token format")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def validate_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("invalid invitation digest")
    return value


class RoomRepository:
    """Internal service boundary, not an HTTP authorization layer.

    actor must come from a verified account session; generation is created by
    the signaling server, never accepted directly from an untrusted client.
    A single standalone Redis aggregate is deliberately bounded at 128 rooms.
    """

    def __init__(
        self, redis: RedisClient, *, limits: Limits | None = None, key: str = "zvonilka:{rooms}:v1"
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9:{}_-]{1,100}", key):
            raise ValueError("invalid Redis key")
        self.redis = redis
        self.limits = limits or Limits()
        self.key = key
        self.script = files("zvonilka.rooms").joinpath("transaction.lua").read_text()

    async def _call(
        self, op: str, actor: str | None = None, room_id: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        request = {"op": op, "limits": asdict(self.limits), **extra}
        if actor is not None:
            request["actor"] = canonical_id(actor)
        if room_id is not None:
            request["room_id"] = canonical_id(room_id)
        response = await self.redis.eval(
            self.script, 1, self.key, json.dumps(request, separators=(",", ":"))
        )
        result = json.loads(response)
        if not result["ok"]:
            raise RoomError(result["code"])
        # Redis cjson encodes an empty Lua table as {}, not [].
        return result["value"]

    async def create(self, actor: str, *, room_id: str | None = None) -> dict:
        return await self._call("create", actor, room_id or str(uuid4()))

    async def snapshot(self, actor: str, room_id: str) -> dict:
        return await self._call("snapshot", actor, room_id)

    async def issue_invite(self, actor: str, room_id: str) -> dict:
        token = secrets.token_urlsafe(32)
        value = await self._call("issue_invite", actor, room_id, digest=invite_digest(token))
        return {"token": token, **value}

    async def revoke_invite(self, actor: str, room_id: str, digest: str) -> dict:
        return await self._call("revoke_invite", actor, room_id, digest=validate_digest(digest))

    async def redeem(self, actor: str, room_id: str, token: str) -> dict:
        return await self._call("redeem", actor, room_id, digest=invite_digest(token))

    async def join(
        self, actor: str, room_id: str, generation: str, *, overloaded: bool = False
    ) -> dict:
        if type(overloaded) is not bool:
            raise ValueError("overloaded must be bool")
        return await self._call(
            "join", actor, room_id, generation=canonical_id(generation), overloaded=overloaded
        )

    async def heartbeat(self, actor: str, room_id: str, generation: str) -> dict:
        return await self._call("heartbeat", actor, room_id, generation=canonical_id(generation))

    async def leave(self, actor: str, room_id: str, generation: str) -> dict:
        return await self._call("leave", actor, room_id, generation=canonical_id(generation))

    async def close(self, actor: str, room_id: str) -> dict:
        return await self._call("close", actor, room_id)

    async def sweep(self) -> dict:
        return await self._call("sweep")
