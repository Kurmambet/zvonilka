"""Versioned copy/paste invitations. Tokens stay out of paths and query strings."""

from dataclasses import dataclass
from uuid import UUID

from .repository import invite_digest


@dataclass(frozen=True, slots=True, repr=False)
class Invitation:
    room_id: UUID
    token: str

    def __repr__(self) -> str:
        return "Invitation(<redacted>)"


def encode_invitation(room_id: UUID, token: str) -> str:
    invite_digest(token)
    return f"zv1.{room_id}.{token}"


def decode_invitation(value: object) -> Invitation:
    # This is an external-input parser: accepting object makes its actual
    # rejection of non-string values part of the public typed contract.
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("invalid invitation")
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "zv1":
        raise ValueError("invalid invitation")
    try:
        room_id = UUID(parts[1])
        if str(room_id) != parts[1]:
            raise ValueError("noncanonical room ID")
        invite_digest(parts[2])
    except (ValueError, AttributeError):
        raise ValueError("invalid invitation") from None
    return Invitation(room_id, parts[2])
