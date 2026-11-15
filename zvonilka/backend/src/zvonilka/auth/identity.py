import hashlib
import re
import secrets
import unicodedata
from uuid import UUID


def normalize_username(value: str) -> str:
    if not value.isascii():
        raise ValueError("username must use ASCII characters")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,31}", value, flags=re.ASCII):
        raise ValueError(
            "username must be 3..32 ASCII characters: a-z, 0-9, underscore; starts with a letter"
        )
    return value


def validate_password(value: str) -> str:
    if not 12 <= len(value) <= 128 or len(value.encode("utf-8")) > 512:
        raise ValueError("password must contain 12..128 characters and at most 512 UTF-8 bytes")
    return value


def validate_display_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 64 or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value):
        raise ValueError("display name must contain 1..64 characters without control characters")
    return value


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def refresh_digest(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token, flags=re.ASCII):
        raise ValueError("invalid refresh token format")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def canonical_uuid(value: str) -> str:
    return str(UUID(value))
