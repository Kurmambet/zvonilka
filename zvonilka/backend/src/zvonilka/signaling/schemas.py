"""Initial signaling v1 contract. Validation is not authentication or admission."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from zvonilka.rooms.schemas import RoomView

PROTOCOL_VERSION = 1
MAX_ACCESS_TOKEN_LENGTH = 4096
MAX_CAPABILITIES = 16

Capability = Annotated[
    str, StringConstraints(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_.-]*$")
]
ApplicationVersion = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$")
]
ErrorCode = Literal[
    "invalid_message",
    "unsupported_version",
    "authentication_required",
    "authentication_timeout",
    "invalid_access",
    "room_not_found",
    "room_closed",
    "not_admitted",
    "room_limit",
    "owner_room_limit",
    "participant_limit",
    "overloaded",
    "registry_full",
    "stale_generation",
    "rate_limited",
    "message_too_large",
    "dependency_unavailable",
    "internal_error",
    "heartbeat_timeout",
    "connection_replaced",
    "server_shutdown",
    "peer_unavailable",
    "peer_replaced",
    "peer_overloaded",
    "invalid_target",
]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        return value


class Authenticate(Message):
    type: Literal["authenticate"]
    request_id: UUID
    room_id: UUID
    access_token: SecretStr = Field(min_length=1, max_length=MAX_ACCESS_TOKEN_LENGTH)
    app_version: ApplicationVersion
    capabilities: list[Capability] = Field(max_length=MAX_CAPABILITIES)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate capability")
        return value


class Joined(Message):
    type: Literal["joined"]
    request_id: UUID
    user_id: UUID
    generation: UUID
    room: RoomView
    capabilities: list[Capability] = Field(max_length=MAX_CAPABILITIES)
    heartbeat_interval_ms: int = Field(default=10000, ge=1, strict=True)
    heartbeat_timeout_ms: int = Field(default=30000, ge=2, strict=True)

    @model_validator(mode="after")
    def online_member(self) -> "Joined":
        if self.heartbeat_interval_ms >= self.heartbeat_timeout_ms:
            raise ValueError("heartbeat interval must be shorter than timeout")
        if self.room.state != "active":
            raise ValueError("joined requires an active room")
        ids = [member.user_id for member in self.room.members]
        if len(ids) != len(set(ids)) or not 1 <= len(ids) <= 3:
            raise ValueError("invalid room members")
        if not any(
            member.user_id == self.user_id and member.online for member in self.room.members
        ):
            raise ValueError("joined user must be online in the snapshot")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("duplicate capability")
        return self


class Error(Message):
    type: Literal["error"]
    request_id: UUID | None
    code: ErrorCode
    fatal: bool = Field(strict=True)


class Heartbeat(Message):
    type: Literal["heartbeat"]
    request_id: UUID
    generation: UUID


class HeartbeatAck(Message):
    type: Literal["heartbeat_ack"]
    request_id: UUID
    generation: UUID


class OnlinePeer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    generation: UUID


class Participants(Message):
    type: Literal["participants"]
    room_id: UUID
    epoch: UUID
    sequence: int = Field(ge=1, strict=True)
    members: list[OnlinePeer] = Field(max_length=3)

    @model_validator(mode="after")
    def unique_members(self) -> "Participants":
        ids = [member.user_id for member in self.members]
        generations = [member.generation for member in self.members]
        if len(ids) != len(set(ids)) or len(generations) != len(set(generations)):
            raise ValueError("duplicate participant or generation")
        return self


MAX_SDP_BYTES = 16384
MAX_ICE_BYTES = 2048


def bounded_utf8(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) > limit:
        raise ValueError("text exceeds byte limit")
    return value


# Opaque payloads: enforce size, not SDP grammar or negotiation state.
Sdp = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SDP_BYTES)]


class IceCandidateData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: str = Field(min_length=1, max_length=MAX_ICE_BYTES, repr=False)
    sdp_mid: str | None = Field(default=None, max_length=64)
    sdp_mline_index: int | None = Field(default=None, ge=0, le=65535, strict=True)
    username_fragment: str | None = Field(default=None, max_length=256)

    @field_validator("candidate")
    @classmethod
    def bounded_candidate(cls, value: str) -> str:
        return bounded_utf8(value, MAX_ICE_BYTES)

    @model_validator(mode="after")
    def media_section(self) -> "IceCandidateData":
        if self.sdp_mid is None and self.sdp_mline_index is None:
            raise ValueError("candidate needs a media section")
        return self


# The client chooses a destination, never the identity forwarded as sender.
class DirectedMessage(Message):
    request_id: UUID
    generation: UUID
    target_user_id: UUID
    target_generation: UUID


class Offer(DirectedMessage):
    type: Literal["offer"]
    sdp: Sdp = Field(repr=False)

    @field_validator("sdp")
    @classmethod
    def bounded_sdp(cls, value: str) -> str:
        return bounded_utf8(value, MAX_SDP_BYTES)


class Answer(DirectedMessage):
    type: Literal["answer"]
    sdp: Sdp = Field(repr=False)

    @field_validator("sdp")
    @classmethod
    def bounded_sdp(cls, value: str) -> str:
        return bounded_utf8(value, MAX_SDP_BYTES)


class IceCandidate(DirectedMessage):
    type: Literal["ice_candidate"]
    # Null, rather than an empty candidate object, explicitly ends gathering.
    candidate: IceCandidateData | None = Field(repr=False)


class RelayedMessage(Message):
    request_id: UUID
    room_id: UUID
    from_user_id: UUID
    from_generation: UUID
    target_generation: UUID


class RelayedOffer(RelayedMessage):
    type: Literal["offer"]
    sdp: Sdp = Field(repr=False)

    @field_validator("sdp")
    @classmethod
    def bounded_sdp(cls, value: str) -> str:
        return bounded_utf8(value, MAX_SDP_BYTES)


class RelayedAnswer(RelayedMessage):
    type: Literal["answer"]
    sdp: Sdp = Field(repr=False)

    @field_validator("sdp")
    @classmethod
    def bounded_sdp(cls, value: str) -> str:
        return bounded_utf8(value, MAX_SDP_BYTES)


class RelayedIceCandidate(RelayedMessage):
    type: Literal["ice_candidate"]
    candidate: IceCandidateData | None = Field(repr=False)


RelayMessage = Offer | Answer | IceCandidate
ActiveMessage = Heartbeat | Offer | Answer | IceCandidate
active_message_adapter = TypeAdapter(Annotated[ActiveMessage, Field(discriminator="type")])
client_message_adapter = TypeAdapter(
    Annotated[Authenticate | Heartbeat | Offer | Answer | IceCandidate, Field(discriminator="type")]
)
server_message_adapter = TypeAdapter(
    Annotated[
        Joined
        | HeartbeatAck
        | Participants
        | Error
        | RelayedOffer
        | RelayedAnswer
        | RelayedIceCandidate,
        Field(discriminator="type"),
    ]
)
