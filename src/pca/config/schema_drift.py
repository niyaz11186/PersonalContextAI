"""SchemaDriftCheck — verifies the live schema matches our declarations.

Cross-cutting. Closes the ADR-009 boundary rule.

ADR-004 makes the numbered `.sql` files the schema authority, and ADR-009 declares
SQLAlchemy `Table` metadata for query building only. Nothing otherwise guarantees the
two agree. Without this check, a column present in `tables.py` but absent from any
migration produces a runtime `UndefinedColumn` error on whichever query happens to
touch it first — possibly weeks later, possibly in the middle of a conversation.

This is the safety property Alembic would have provided by generating one from the
other. Since the user chose raw SQL, it is recovered by comparison instead.

**Names only, not types.** Type comparison across SQLAlchemy and PostgreSQL is
brittle enough that it produces false alarms, and a test people learn to ignore is
worse than no test. A missing table or column is the failure that actually happens.
"""

from __future__ import annotations

from sqlalchemy import text

from pca.adapters.postgres.tables import metadata
from pca.domain.errors import ConfigurationError
from pca.observability.logging import get_logger
from pca.ports.store import RelationalStorePort

_log = get_logger(__name__)

_LIVE_COLUMNS = text(
    """
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
    """
)


class SchemaDriftCheck:
    def __init__(self, store: RelationalStorePort) -> None:
        self._store = store

    async def live_schema(self) -> dict[str, set[str]]:
        rows = await self._store.fetch_all(_LIVE_COLUMNS)
        found: dict[str, set[str]] = {}
        for row in rows:
            found.setdefault(row["table_name"], set()).add(row["column_name"])
        return found

    async def assert_matches(self) -> None:
        """Fail loudly on drift.

        Raises rather than warns. A schema mismatch means an unknown subset of
        queries is broken, and starting anyway would surface that as scattered
        runtime errors rather than one clear statement at boot.
        """
        live = await self.live_schema()

        missing_tables: list[str] = []
        missing_columns: list[str] = []

        for name, table in metadata.tables.items():
            if name not in live:
                missing_tables.append(name)
                continue
            declared = {column.name for column in table.columns}
            absent = declared - live[name]
            missing_columns.extend(f"{name}.{column}" for column in sorted(absent))

        if missing_tables or missing_columns:
            details: list[str] = []
            if missing_tables:
                details.append(f"tables absent from the database: {sorted(missing_tables)}")
            if missing_columns:
                details.append(f"columns absent from the database: {sorted(missing_columns)}")
            raise ConfigurationError(
                "schema drift detected — declared metadata does not match the live "
                "database. "
                + "; ".join(details)
                + ". The .sql migrations are authoritative (ADR-004): add a migration "
                "rather than editing tables.py to match."
            )

        # Extra tables and columns in the database are reported but not fatal. They
        # are the expected state midway through a change, and Graphiti's own Neo4j
        # schema aside, nothing here owns the whole database.
        extra = set(live) - set(metadata.tables)
        if extra:
            _log.info("schema_has_undeclared_tables", tables=sorted(extra))

        _log.info("schema_drift_check_passed", tables=len(metadata.tables))
