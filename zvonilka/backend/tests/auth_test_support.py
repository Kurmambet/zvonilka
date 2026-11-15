import os
import secrets
import unittest
from uuid import uuid4

PG_URL = os.environ.get("ZVONILKA_TEST_DATABASE_URL")


class AuthPgFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pydantic import SecretStr
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from zvonilka.auth.jwt_tokens import AccessTokens
        from zvonilka.auth.passwords import Passwords
        from zvonilka.auth.service import AuthService
        from zvonilka.config import Settings
        from zvonilka.db import Base, create_database

        assert PG_URL is not None
        self.schema = "auth_test_" + uuid4().hex
        self.admin = create_async_engine(PG_URL, hide_parameters=True)
        self.addAsyncCleanup(self.admin.dispose)
        async with self.admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))

        async def drop_schema():
            async with self.admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

        self.addAsyncCleanup(drop_schema)
        self.settings = Settings(
            database_url=SecretStr(PG_URL),
            database_schema=self.schema,
            jwt_secret=SecretStr(secrets.token_urlsafe(48)),
            rate_limit_prefix=self.schema + ":",
        )
        self.engine, self.sessions = create_database(self.settings)
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.passwords = Passwords(memory_cost=8192, time_cost=1)
        await self.passwords.initialize()
        self.access = AccessTokens(
            self.settings.jwt_secret.get_secret_value(), "test-issuer", "test-audience"
        )
        self.service = AuthService(self.sessions, self.passwords, self.access)
        self.password = "my-original-password-123"

    async def register(self, username="alice"):
        return await self.service.register(username, username.title(), self.password)

    async def login(self, username="alice"):
        return await self.service.login(username, self.password)

    async def assert_auth_error(self, code, operation):
        from zvonilka.auth.errors import AuthError

        with self.assertRaises(AuthError) as ctx:
            await operation
        self.assertEqual(ctx.exception.code, code)
