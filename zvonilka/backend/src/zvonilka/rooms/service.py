from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from .invitations import decode_invitation, encode_invitation
from .repository import invite_digest


class Rooms(Protocol):
    async def create(self, actor: str) -> dict: ...
    async def snapshot(self, actor: str, room_id: str) -> dict: ...
    async def issue_invite(self, actor: str, room_id: str) -> dict: ...
    async def revoke_invite(self, actor: str, room_id: str, digest: str) -> dict: ...
    async def redeem(self, actor: str, room_id: str, token: str) -> dict: ...
    async def close(self, actor: str, room_id: str) -> dict: ...


class RoomService:
    """actor is supplied by the verified Principal, not by request JSON."""

    def __init__(self, repository: Rooms):
        self.repository = repository

    async def create(self, actor: UUID) -> dict:
        return await self.repository.create(str(actor))

    async def snapshot(self, actor: UUID, room_id: UUID) -> dict:
        return await self.repository.snapshot(str(actor), str(room_id))

    async def issue_invite(self, actor: UUID, room_id: UUID) -> dict:
        value = await self.repository.issue_invite(str(actor), str(room_id))
        token = value["token"]
        return {
            "invite_id": invite_digest(token),
            "room_id": room_id,
            "invitation": encode_invitation(room_id, token),
            "expires_at": datetime.fromtimestamp(value["expires_at"], UTC),
        }

    async def revoke_invite(self, actor: UUID, room_id: UUID, invite_id: str) -> None:
        await self.repository.revoke_invite(str(actor), str(room_id), invite_id)

    async def redeem(self, actor: UUID, invitation: str) -> dict:
        value = decode_invitation(invitation)
        return await self.repository.redeem(str(actor), str(value.room_id), value.token)

    async def close(self, actor: UUID, room_id: UUID) -> None:
        await self.repository.close(str(actor), str(room_id))
