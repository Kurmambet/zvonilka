import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from zvonilka.auth import models  # noqa: F401 - register ORM metadata
from zvonilka.config import Settings
from zvonilka.db import Base

# Required values are loaded and validated from environment/.env.
settings = Settings()  # pyright: ignore[reportCallIssue]
target_metadata = Base.metadata


def configure(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table_schema=settings.database_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": settings.database_schema}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations not supported in this stage")
else:
    asyncio.run(run_online())
