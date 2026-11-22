import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZVONILKA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    environment: Literal["dev", "staging", "production"] = "dev"

    database_url: SecretStr
    database_schema: str = "public"
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    jwt_secret: SecretStr
    rate_limit_prefix: str = Field(
        default="zvonilka:auth:rate:", pattern=r"^[a-zA-Z0-9:_-]{1,100}$"
    )
    jwt_issuer: str = "zvonilka"
    jwt_audience: str = "zvonilka-api-v1"
    access_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    refresh_ttl_seconds: int = Field(default=1209600, ge=3600, le=2592000)
    max_refresh_tokens_per_session: int = Field(default=4096, ge=128, le=8192)
    max_sessions_per_user: int = Field(default=10, ge=1, le=50)
    auth_body_limit_bytes: int = Field(default=16384, ge=1024, le=65536)
    database_pool_size: int = Field(default=3, ge=1, le=10)

    # Temporary room state is a separate bounded Redis aggregate.
    room_registry_key: str = Field(
        default="zvonilka:{rooms}:v1", pattern=r"^[a-zA-Z0-9:{}_-]{1,100}$"
    )
    admission_mode: Literal["static"] = "static"
    max_participants_per_room: int = Field(default=3, ge=2, le=3)
    max_active_rooms: int = Field(default=2, ge=1, le=128)
    max_active_rooms_per_user: int = Field(default=1, ge=1, le=128)
    max_pending_rooms_per_user: int = Field(default=3, ge=1, le=128)
    room_pending_ttl_seconds: int = Field(default=86400, ge=1, le=604800)
    room_empty_grace_seconds: int = Field(default=120, ge=1, le=3600)
    room_max_lifetime_seconds: int = Field(default=43200, ge=1, le=86400)
    invite_ttl_seconds: int = Field(default=86400, ge=1, le=604800)
    room_presence_ttl_seconds: int = Field(default=45, ge=1, le=300)
    room_closed_retention_seconds: int = Field(default=86400, ge=1, le=604800)
    room_registry_max_records: int = Field(default=128, ge=1, le=128)
    room_invites_per_room: int = Field(default=32, ge=1, le=32)
    room_create_rate_limit: int = Field(default=12, ge=1, le=1000)
    room_read_rate_limit: int = Field(default=120, ge=1, le=10000)
    room_write_rate_limit: int = Field(default=30, ge=1, le=1000)
    invite_redeem_rate_limit: int = Field(default=30, ge=1, le=1000)

    def room_limits(self):
        from .rooms.repository import Limits

        return Limits(
            rooms=self.max_active_rooms,
            owner_rooms=self.max_active_rooms_per_user,
            participants=self.max_participants_per_room,
            pending_per_owner=self.max_pending_rooms_per_user,
            pending_ttl=self.room_pending_ttl_seconds,
            grace=self.room_empty_grace_seconds,
            lifetime=self.room_max_lifetime_seconds,
            invite_ttl=self.invite_ttl_seconds,
            presence_ttl=self.room_presence_ttl_seconds,
            closed_retention=self.room_closed_retention_seconds,
            max_records=self.room_registry_max_records,
            invites_per_room=self.room_invites_per_room,
        )

    @field_validator("database_schema")
    @classmethod
    def schema_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
            raise ValueError("invalid PostgreSQL schema name")
        return value

    @field_validator("database_url")
    @classmethod
    def database_valid(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database URL must use postgresql+asyncpg")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def secret_valid(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32 or value.get_secret_value().startswith(
            "REPLACE_"
        ):
            raise ValueError("JWT secret must contain at least 32 bytes")
        return value

    @model_validator(mode="after")
    def lifetimes_valid(self):
        if self.refresh_ttl_seconds <= self.access_ttl_seconds:
            raise ValueError("refresh TTL must exceed access TTL")
        return self
