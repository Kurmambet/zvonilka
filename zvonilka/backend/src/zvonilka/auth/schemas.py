from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .identity import normalize_username, validate_display_name, validate_password


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class RegisterInput(Input):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def username_valid(cls, value: str) -> str:
        return normalize_username(value)

    @field_validator("display_name")
    @classmethod
    def display_valid(cls, value: str) -> str:
        return validate_display_name(value)

    @field_validator("password")
    @classmethod
    def password_valid(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


class LoginInput(Input):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def username_valid(cls, value: str) -> str:
        return normalize_username(value)


class RefreshInput(Input):
    refresh_token: SecretStr = Field(min_length=43, max_length=43)


class ChangePasswordInput(Input):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_valid(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


class Profile(BaseModel):
    id: UUID
    username: str
    display_name: str
    created_at: datetime


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_at: datetime
    session_id: UUID


class SessionView(BaseModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool
