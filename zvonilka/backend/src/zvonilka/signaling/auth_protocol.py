"""Reauthentication contract; wired into the WS dispatcher in the next delivery."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, TypeAdapter

from .schemas import MAX_ACCESS_TOKEN_LENGTH, Message


class Reauthenticate(Message):
    type: Literal["reauthenticate"]
    request_id: UUID
    generation: UUID
    access_token: SecretStr = Field(min_length=1, max_length=MAX_ACCESS_TOKEN_LENGTH)


class Reauthenticated(Message):
    type: Literal["reauthenticated"]
    request_id: UUID
    generation: UUID
    expires_at: int = Field(strict=True, ge=1)


class ReauthRequired(Message):
    type: Literal["reauth_required"]
    generation: UUID


reauthentication_client_adapter = TypeAdapter(Reauthenticate)
reauthentication_server_adapter = TypeAdapter(
    Annotated[Reauthenticated | ReauthRequired, Field(discriminator="type")]
)
