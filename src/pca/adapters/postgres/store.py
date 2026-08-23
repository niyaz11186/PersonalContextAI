"""PostgresStoreAdapter — implements RelationalStorePort.

Layer L5. The only module permitted to import SQLAlchemy engine machinery.

ADR-009: SQLAlchemy Core over asyncpg. Core rather than the ORM so there is no
identity map, no lazy loading, and no temptation to treat model classes as the
schema source of truth — the `.sql` files hold that role (ADR-004).

Constraint C-22: PostgreSQL is the system of record and has no degradation path.
Failures here raise `SourceOfRecordUnavailable` rather than returning empty
results, because silently answering from nothing is worse than failing.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from pca.domain.errors import SourceOfRecordUnavailable
from pca.observability.logging import get_logger

_log = get_logger(__name__)


def to_asyncpg_dsn(dsn: str) -> str:
    """Normalise a DSN to the asyncpg dialect.

    Configuration carries the plain `postgresql://` form because that is what
    psql, pg_dump, and every operator tool expect. SQLAlchemy needs the driver
    named explicitly, so translate rather than forcing the operator-facing value
    to be unusual.
    """
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn


class _ConnectionTransaction:
    """Transaction scope bound to a single connection."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def execute(self, statement: Any, parameters: Any | None = None) -> Any:
        return await self._connection.execute(statement, parameters)

    async def fetch_all(self, statement: Any, parameters: Any | None = None) -> Sequence[Any]:
        result = await self._connection.execute(statement, parameters)
        return result.mappings().all()

    async def fetch_one(self, statement: Any, parameters: Any | None = None) -> Any | None:
        result = await self._connection.execute(statement, parameters)
        return result.mappings().first()


class PostgresStoreAdapter:
    """PostgreSQL-backed RelationalStorePort."""

    def __init__(self, dsn: str, echo: bool = False, pool_size: int = 5) -> None:
        self._engine: AsyncEngine = create_async_engine(
            to_asyncpg_dsn(dsn),
            echo=echo,
            pool_size=pool_size,
            max_overflow=2,
            pool_pre_ping=True,  # a stale pooled connection should not fail a request
        )

    # ------------------------------------------------------------------ public

    async def execute(self, statement: Any, parameters: Any | None = None) -> Any:
        async with self._connect() as connection:
            return await connection.execute(statement, parameters)

    async def fetch_all(self, statement: Any, parameters: Any | None = None) -> Sequence[Any]:
        async with self._connect() as connection:
            result = await connection.execute(statement, parameters)
            return result.mappings().all()

    async def fetch_one(self, statement: Any, parameters: Any | None = None) -> Any | None:
        async with self._connect() as connection:
            result = await connection.execute(statement, parameters)
            return result.mappings().first()

    @asynccontextmanager
    async def transaction(self):
        """Atomic scope across several repositories.

        Required because a memory commit spans memory rows, the operation log,
        and belief records. Graph ingestion deliberately happens *after* this
        commit returns — PostgreSQL is the durability point (ADR-005).
        """
        try:
            async with self._engine.begin() as connection:
                yield _ConnectionTransaction(connection)
        except SQLAlchemyError as exc:
            _log.error("postgres_transaction_failed", error=str(exc)[:300])
            raise SourceOfRecordUnavailable(f"PostgreSQL transaction failed: {exc}") from exc

    async def execute_script(self, sql: str) -> None:
        """Run multi-statement DDL. Used only by MigrationRunner."""
        try:
            async with self._engine.begin() as connection:
                await connection.exec_driver_sql(sql)
        except SQLAlchemyError as exc:
            _log.error("postgres_script_failed", error=str(exc)[:300])
            raise SourceOfRecordUnavailable(f"PostgreSQL script failed: {exc}") from exc

    async def health(self) -> bool:
        try:
            async with self._connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - health must never raise
            _log.warning("postgres_unhealthy", error=str(exc)[:200])
            return False

    async def close(self) -> None:
        await self._engine.dispose()

    # --------------------------------------------------------------- internals

    @asynccontextmanager
    async def _connect(self):
        try:
            async with self._engine.begin() as connection:
                yield connection
        except SQLAlchemyError as exc:
            _log.error("postgres_unavailable", error=str(exc)[:300])
            raise SourceOfRecordUnavailable(f"PostgreSQL unavailable: {exc}") from exc
