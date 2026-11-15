import re

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZVONILKA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
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
