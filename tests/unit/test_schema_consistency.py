"""Offline equivalent of SchemaDriftCheck.

`SchemaDriftCheck` compares declared metadata against a live database, which means it
can only run once PostgreSQL exists. This file performs the same comparison against
the `.sql` migration files, so drift is caught here rather than at startup on the
machine that actually has Docker.

That distinction matters practically: development happens on a machine with no
container runtime, so a mismatch introduced here would otherwise stay invisible until
someone tried to boot the application elsewhere and lost a session to it.

Text-based and deliberately crude. It answers "is every declared column mentioned in
a CREATE TABLE for that table?" — which is the drift that actually happens when a
column is added to `tables.py` and forgotten in the migration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pca.adapters.postgres.tables import metadata

MIGRATIONS = Path("migrations")


def migration_sql() -> str:
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert files, "expected migration files"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def create_block(sql: str, table: str) -> str | None:
    """The body of the CREATE TABLE statement for `table`, if present."""
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table)}\s*\((.*?)\n\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    return match.group(1) if match else None


def added_columns(sql: str, table: str) -> set[str]:
    """Columns introduced by `ALTER TABLE ... ADD COLUMN` after the table was created.

    Necessary because ADR-004 makes migrations forward-only: a column added to an
    existing table CANNOT be edited into its original CREATE TABLE, so it will only
    ever appear in an ALTER. Checking the CREATE block alone would report every such
    column as missing and push someone toward rewriting an applied migration — the one
    thing the forward-only rule exists to prevent.
    """
    pattern = re.compile(
        rf"ALTER\s+TABLE\s+{re.escape(table)}\s+ADD\s+COLUMN\s+"
        rf"(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        re.IGNORECASE,
    )
    return {match.group(1) for match in pattern.finditer(sql)}


@pytest.fixture(scope="module")
def sql() -> str:
    return migration_sql()


def test_every_declared_table_exists_in_a_migration(sql: str) -> None:
    missing = [name for name in metadata.tables if create_block(sql, name) is None]

    assert not missing, (
        f"tables declared in tables.py with no CREATE TABLE in migrations: {missing}. "
        "The .sql files are authoritative (ADR-004) — add a migration."
    )


@pytest.mark.parametrize("table_name", sorted(metadata.tables))
def test_every_declared_column_exists_in_the_migration(table_name: str, sql: str) -> None:
    """Catches the common drift: a column added to tables.py, forgotten in SQL.

    Without this, the mismatch surfaces as an UndefinedColumn error from whichever
    query touches it first — possibly weeks later, possibly mid-conversation.
    """
    block = create_block(sql, table_name)
    assert block is not None, f"no CREATE TABLE for {table_name}"

    altered = added_columns(sql, table_name)
    declared = {column.name for column in metadata.tables[table_name].columns}
    # Word-boundary match so `valid_to` is not satisfied by `valid_from`.
    absent = {
        column
        for column in declared
        if column not in altered
        and not re.search(rf"\b{re.escape(column)}\b", block)
    }

    assert not absent, (
        f"{table_name}: columns declared in tables.py but not found in the migration: "
        f"{sorted(absent)}"
    )


def strip_sql_comments(sql: str) -> str:
    """Remove `--` comments.

    Necessary because the migrations explain their own reasoning in prose, and words
    like "timestamp" appear there legitimately. Scanning raw text produced false
    positives — and a test that cries wolf is worse than no test at all.
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def test_no_table_uses_timestamp_without_timezone(sql: str) -> None:
    """ADR-011: every instant is a UTC instant.

    A bare `TIMESTAMP` column would silently drop the offset, and ordering across a
    DST transition would then be wrong in a way that is very hard to notice.

    Case-sensitive and comment-stripped: type declarations are written in upper case,
    so matching case-insensitively across prose only finds English.
    """
    code = strip_sql_comments(sql)
    offenders = re.findall(r"\bTIMESTAMP\b(?!\s*(?:TZ|WITH))", code)

    assert not offenders, (
        f"found {len(offenders)} bare TIMESTAMP declaration(s); ADR-011 requires "
        "TIMESTAMPTZ throughout"
    )


def test_every_timestamp_column_is_timestamptz(sql: str) -> None:
    """The positive form of the check above."""
    code = strip_sql_comments(sql)
    assert code.count("TIMESTAMPTZ") >= 20, (
        "expected many TIMESTAMPTZ columns; if this dropped sharply, check whether "
        "timestamps are being declared some other way"
    )


def test_migrations_do_not_manage_their_own_transactions(sql: str) -> None:
    """MigrationRunner wraps each file; an inner BEGIN/COMMIT would nest."""
    assert "BEGIN;" not in sql.upper()
    assert "COMMIT;" not in sql.upper()


def test_declared_metadata_never_creates_the_schema() -> None:
    """ADR-009 boundary rule.

    `metadata.create_all()` would make tables.py the de facto schema authority and
    quietly bypass the migration history. The absence of that call is the rule.

    Uses AST rather than text search. Several modules *document* that create_all is
    never called, and a plain substring scan flagged those docstrings — the same
    false-positive trap as the datetime.now() guard in test_privacy_guards.py.
    """
    import ast

    offenders: list[str] = []
    for path in Path("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in {
                    "create_all",
                    "drop_all",
                }:
                    offenders.append(f"{path}:{node.lineno} .{func.attr}()")

    assert not offenders, f"schema-mutating metadata calls found: {offenders}"
