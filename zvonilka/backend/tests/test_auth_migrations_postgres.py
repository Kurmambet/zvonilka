import asyncio
import os
import secrets
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4

PG_URL = os.environ.get("ZVONILKA_TEST_DATABASE_URL")


@unittest.skipUnless(PG_URL, "real PostgreSQL URL is required")
class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_clean_schema_and_repeated_upgrade(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        assert PG_URL is not None
        engine = create_async_engine(PG_URL, poolclass=NullPool, hide_parameters=True)
        schema = "auth_migration_" + uuid4().hex
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            env = {
                **os.environ,
                "ZVONILKA_DATABASE_URL": PG_URL,
                "ZVONILKA_DATABASE_SCHEMA": schema,
                "ZVONILKA_JWT_SECRET": secrets.token_urlsafe(48),
            }
            script = Path(__file__).resolve().parents[2] / "scripts/migrate-auth.py"
            for _ in range(2):
                result = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, str(script)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("MIGRATION_OK", result.stdout)
            async with engine.connect() as connection:
                version = await connection.scalar(
                    text(f'SELECT version_num FROM "{schema}".alembic_version')
                )
                self.assertEqual(version, "0001_auth")
                tables = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT table_name FROM information_schema.tables WHERE table_schema=:schema"
                            ),
                            {"schema": schema},
                        )
                    ).all()
                )
                self.assertEqual(
                    tables,
                    {"alembic_version", "auth_users", "auth_sessions", "auth_refresh_tokens"},
                )
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await engine.dispose()
