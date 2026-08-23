"""MigrationRunner — applies numbered raw SQL migrations.

Cross-cutting. ADR-004.

No Alembic, by user directive. What Alembic mainly provides and this replaces:

  - ordered application of pending changes  -> numbered filenames
  - a record of what has been applied       -> schema_migrations table
  - protection against edited history       -> checksum verification

What is deliberately NOT replaced: downgrade paths and autogeneration. Migrations
are forward-only. A mistake in an applied file is corrected by a NEW migration,
never by editing the old one — which is precisely what checksum verification
enforces.

The numbered-file convention is the same one Alembic, Flyway, and sqlx-migrate
expect, so adopting a tool later means writing a config file rather than
rewriting migrations.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, select

from pca.adapters.postgres.tables import schema_migrations
from pca.domain.errors import ConfigurationError
from pca.observability.logging import get_logger
from pca.ports.clock import ClockPort
from pca.ports.store import RelationalStorePort

_log = get_logger(__name__)

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True, slots=True)
class MigrationFile:
    version: str
    path: Path
    sql: str
    checksum: str


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: str
    applied_at: datetime


def checksum_of(sql: str) -> str:
    """Content hash, insensitive to line-ending differences.

    Normalising newlines matters on Windows: a file checked out with CRLF would
    otherwise appear modified relative to one applied from LF, producing a false
    drift alarm on the very machine this project is being moved between.
    """
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


class MigrationRunner:
    """Discovers, verifies, and applies migration files."""

    def __init__(
        self,
        store: RelationalStorePort,
        clock: ClockPort,
        migrations_dir: Path | str = "migrations",
    ) -> None:
        self._store = store
        self._clock = clock
        self._dir = Path(migrations_dir)

    # ------------------------------------------------------------------ public

    def discover(self) -> list[MigrationFile]:
        """Load migration files in version order.

        Rejects unexpected filenames rather than skipping them. A file named
        `fix_thing.sql` sitting unapplied and unnoticed is a worse outcome than a
        loud startup failure.
        """
        if not self._dir.is_dir():
            raise ConfigurationError(f"migrations directory not found: {self._dir}")

        found: list[MigrationFile] = []
        for path in sorted(self._dir.iterdir()):
            if path.is_dir() or path.suffix != ".sql":
                continue
            if not _FILENAME.match(path.name):
                raise ConfigurationError(
                    f"migration filename {path.name!r} does not match NNNN_lower_snake.sql"
                )
            sql = path.read_text(encoding="utf-8")
            found.append(
                MigrationFile(
                    version=path.name.split("_", 1)[0],
                    path=path,
                    sql=sql,
                    checksum=checksum_of(sql),
                )
            )

        versions = [m.version for m in found]
        duplicates = {v for v in versions if versions.count(v) > 1}
        if duplicates:
            raise ConfigurationError(f"duplicate migration versions: {sorted(duplicates)}")

        return found

    async def applied(self) -> dict[str, str]:
        """version -> checksum for everything already applied.

        Returns empty when schema_migrations does not yet exist, which is the
        first-run case.
        """
        try:
            rows = await self._store.fetch_all(
                select(schema_migrations.c.version, schema_migrations.c.checksum)
            )
        except Exception:  # noqa: BLE001 - table absent on a fresh database
            return {}
        return {row["version"]: row["checksum"] for row in rows}

    async def verify_checksums(self) -> None:
        """Fail if an already-applied migration has been edited.

        This is the safety property that not using Alembic would otherwise cost.
        Editing an applied migration means the database and the repository
        disagree about the schema, and every subsequent assumption is unreliable.
        """
        applied = await self.applied()
        if not applied:
            return

        for migration in self.discover():
            recorded = applied.get(migration.version)
            if recorded is None:
                continue
            if recorded != migration.checksum:
                raise ConfigurationError(
                    f"migration {migration.path.name} was modified after being applied "
                    f"(recorded {recorded[:12]}, found {migration.checksum[:12]}). "
                    "Migrations are forward-only: add a new migration instead of editing this one."
                )

        missing = set(applied) - {m.version for m in self.discover()}
        if missing:
            raise ConfigurationError(
                f"database reports migrations with no matching file: {sorted(missing)}"
            )

    async def apply_pending(self) -> list[AppliedMigration]:
        """Apply everything not yet recorded, in order."""
        await self.verify_checksums()

        applied = await self.applied()
        pending = [m for m in self.discover() if m.version not in applied]

        if not pending:
            _log.info("migrations_up_to_date", count=len(applied))
            return []

        results: list[AppliedMigration] = []
        for migration in pending:
            now = self._clock.now()
            _log.info("migration_applying", version=migration.version, file=migration.path.name)

            # Each file runs in its own transaction. Per-file rather than one
            # transaction for all of them, so a failure halfway leaves earlier
            # migrations applied and recorded rather than silently rolled back.
            await self._store.execute_script(migration.sql)
            await self._store.execute(
                insert(schema_migrations).values(
                    version=migration.version,
                    checksum=migration.checksum,
                    applied_at=now,
                )
            )
            results.append(AppliedMigration(version=migration.version, applied_at=now))
            _log.info("migration_applied", version=migration.version)

        return results
