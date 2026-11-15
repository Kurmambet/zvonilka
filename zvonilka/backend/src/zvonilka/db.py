from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import Settings


class Base(DeclarativeBase):
    pass


def create_database(settings: Settings):
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        echo=False,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=1,
        connect_args={"server_settings": {"search_path": settings.database_schema}},
        hide_parameters=True,
    )
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
