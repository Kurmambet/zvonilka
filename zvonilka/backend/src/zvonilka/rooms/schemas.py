from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .invitations import decode_invitation


class CreateRoomInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RedeemInvitationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invitation: SecretStr = Field(min_length=84, max_length=84)

    @field_validator("invitation")
    @classmethod
    def valid_invitation(cls, value: SecretStr) -> SecretStr:
        decode_invitation(value.get_secret_value())
        return value


class RoomMember(BaseModel):
    user_id: UUID
    online: bool


class RoomView(BaseModel):
    room_id: UUID
    owner_id: UUID
    state: Literal["created", "active", "recovering", "closed"]
    revision: int = Field(ge=1)
    members: list[RoomMember]
    activated_at: datetime | None = None
    deadline: datetime | None = None
    empty_at: datetime | None = None


class IssuedInvitation(BaseModel):
    # This response deliberately delivers the secret once to the owner.
    invite_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    room_id: UUID
    invitation: str
    expires_at: datetime
