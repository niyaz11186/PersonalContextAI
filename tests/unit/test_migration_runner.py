"""Tests for MigrationRunner.

These cover the safety properties that not using Alembic would otherwise cost
(ADR-004): ordered application, a record of what ran, and detection of edited
history.

The line-ending test matters concretely right now — this project is about to be
cloned onto another machine, and a checkout that converts LF to CRLF must not
appear as tampered history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pca.config.migrations import MigrationRunner, checksum_of
from pca.domain.errors import ConfigurationError
from tests.fakes.clock import FakeClock
from tests.fakes.store import FakeRelationalStore

START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


# ------------------------------------------------------------------- checksums


def test_checksum_ignores_line_ending_differences() -> None:
    """A CRLF checkout must not look like an edited migration.

    Without normalisation, cloning this repository onto a machine that converts
    line endings would trip the tamper check on every migration and block startup
    for no real reason.
    """
    assert checksum_of("CREATE TABLE a();\nCREATE TABLE b();") == checksum_of(
        "CREATE TABLE a();\r\nCREATE TABLE b();"
    )


def test_checksum_ignores_surrounding_whitespace() -> None:
    assert checksum_of("SELECT 1;") == checksum_of("\n  SELECT 1;\n\n")


def test_checksum_detects_real_content_change() -> None:
    assert checksum_of("SELECT 1;") != checksum_of("SELECT 2;")


# -------------------------------------------------------------------- discover


def test_discover_orders_by_version(tmp_path: Path, clock: FakeClock) -> None:
    write(tmp_path, "0002_second.sql", "SELECT 2;")
    write(tmp_path, "0001_first.sql", "SELECT 1;")
    write(tmp_path, "0010_tenth.sql", "SELECT 10;")

    runner = MigrationRunner(FakeRelationalStore(), clock, tmp_path)

    assert [m.version for m in runner.discover()] == ["0001", "0002", "0010"]


def test_discover_rejects_unexpected_filename(tmp_path: Path, clock: FakeClock) -> None:
    """A stray .sql file must fail loudly rather than be skipped.

    An unapplied, unnoticed `fix_thing.sql` is a worse outcome than a startup error.
    """
    write(tmp_path, "0001_first.sql", "SELECT 1;")
    write(tmp_path, "quickfix.sql", "SELECT 99;")

    runner = MigrationRunner(FakeRelationalStore(), clock, tmp_path)

    with pytest.raises(ConfigurationError, match="does not match"):
        runner.discover()


def test_discover_rejects_duplicate_versions(tmp_path: Path, clock: FakeClock) -> None:
    write(tmp_path, "0001_first.sql", "SELECT 1;")
    write(tmp_path, "0001_also_first.sql", "SELECT 2;")

    runner = MigrationRunner(FakeRelationalStore(), clock, tmp_path)

    with pytest.raises(ConfigurationError, match="duplicate migration versions"):
        runner.discover()


def test_discover_ignores_non_sql_files(tmp_path: Path, clock: FakeClock) -> None:
    write(tmp_path, "0001_first.sql", "SELECT 1;")
    write(tmp_path, "README.md", "notes")
    (tmp_path / "subdir").mkdir()

    runner = MigrationRunner(FakeRelationalStore(), clock, tmp_path)

    assert [m.version for m in runner.discover()] == ["0001"]


def test_missing_directory_raises(tmp_path: Path, clock: FakeClock) -> None:
    runner = MigrationRunner(FakeRelationalStore(), clock, tmp_path / "absent")

    with pytest.raises(ConfigurationError, match="migrations directory not found"):
        runner.discover()


# --------------------------------------------------------------------- applying


async def test_apply_pending_on_fresh_database_applies_everything(
    tmp_path: Path, clock: FakeClock
) -> None:
    write(tmp_path, "0001_first.sql", "CREATE TABLE a();")
    write(tmp_path, "0002_second.sql", "CREATE TABLE b();")
    store = FakeRelationalStore(rows=[])

    runner = MigrationRunner(store, clock, tmp_path)
    applied = await runner.apply_pending()

    assert [a.version for a in applied] == ["0001", "0002"]
    assert store.scripts == ["CREATE TABLE a();", "CREATE TABLE b();"]


async def test_apply_pending_skips_already_applied(tmp_path: Path, clock: FakeClock) -> None:
    body = "CREATE TABLE a();"
    write(tmp_path, "0001_first.sql", body)
    write(tmp_path, "0002_second.sql", "CREATE TABLE b();")
    store = FakeRelationalStore(rows=[{"version": "0001", "checksum": checksum_of(body)}])

    runner = MigrationRunner(store, clock, tmp_path)
    applied = await runner.apply_pending()

    assert [a.version for a in applied] == ["0002"]
    assert store.scripts == ["CREATE TABLE b();"]


async def test_apply_pending_is_noop_when_up_to_date(
    tmp_path: Path, clock: FakeClock
) -> None:
    body = "CREATE TABLE a();"
    write(tmp_path, "0001_first.sql", body)
    store = FakeRelationalStore(rows=[{"version": "0001", "checksum": checksum_of(body)}])

    runner = MigrationRunner(store, clock, tmp_path)

    assert await runner.apply_pending() == []
    assert store.scripts == []


# ------------------------------------------------------------- tamper detection


async def test_editing_an_applied_migration_is_rejected(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The core safety property recovered from Alembic's absence.

    An edited applied migration means the database and the repository disagree
    about the schema, after which every assumption is unreliable.
    """
    write(tmp_path, "0001_first.sql", "CREATE TABLE edited();")
    store = FakeRelationalStore(
        rows=[{"version": "0001", "checksum": checksum_of("CREATE TABLE original();")}]
    )

    runner = MigrationRunner(store, clock, tmp_path)

    with pytest.raises(ConfigurationError, match="modified after being applied"):
        await runner.verify_checksums()


async def test_error_message_directs_toward_a_new_migration(
    tmp_path: Path, clock: FakeClock
) -> None:
    """Forward-only is the rule; the error should say so rather than just fail."""
    write(tmp_path, "0001_first.sql", "CREATE TABLE edited();")
    store = FakeRelationalStore(
        rows=[{"version": "0001", "checksum": checksum_of("CREATE TABLE original();")}]
    )

    runner = MigrationRunner(store, clock, tmp_path)

    with pytest.raises(ConfigurationError, match="forward-only"):
        await runner.verify_checksums()


async def test_applied_migration_with_no_file_is_rejected(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A deleted migration file is as dangerous as an edited one."""
    write(tmp_path, "0001_first.sql", "CREATE TABLE a();")
    store = FakeRelationalStore(
        rows=[
            {"version": "0001", "checksum": checksum_of("CREATE TABLE a();")},
            {"version": "0002", "checksum": "deadbeef"},
        ]
    )

    runner = MigrationRunner(store, clock, tmp_path)

    with pytest.raises(ConfigurationError, match="no matching file"):
        await runner.verify_checksums()


async def test_verify_is_noop_on_fresh_database(tmp_path: Path, clock: FakeClock) -> None:
    write(tmp_path, "0001_first.sql", "CREATE TABLE a();")
    runner = MigrationRunner(FakeRelationalStore(rows=[]), clock, tmp_path)

    await runner.verify_checksums()  # must not raise


# ------------------------------------------------------- the real project files


def test_shipped_migrations_are_wellformed(clock: FakeClock) -> None:
    """The project's own migrations must satisfy the runner's rules.

    Guards against a real file drifting out of convention, and against
    reintroducing the BEGIN/COMMIT that would nest inside the runner's own
    transaction.
    """
    runner = MigrationRunner(FakeRelationalStore(), clock, Path("migrations"))
    discovered = runner.discover()

    assert discovered, "expected at least one migration file"
    assert discovered[0].version == "0001"

    for migration in discovered:
        upper = migration.sql.upper()
        assert "BEGIN;" not in upper, f"{migration.path.name} must not open its own transaction"
        assert "COMMIT;" not in upper, f"{migration.path.name} must not commit; the runner does"
