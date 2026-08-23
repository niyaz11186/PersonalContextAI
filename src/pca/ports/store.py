"""RelationalStorePort — PostgreSQL, the system of record.

Layer L4.

ADR-005 makes PostgreSQL authoritative and Neo4j a rebuildable projection. That
asymmetry is why this port has no degradation path: if it is unavailable the
request fails (constraint C-22).

ADR-009 selects SQLAlchemy Core, not the ORM. Table metadata may be declared for
query building, but `.sql` migration files remain the schema authority and
metadata.create_all() is never called.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol


class Transaction(Protocol):
    async def execute(self, statement: Any, parameters: Any | None = None) -> Any: ...

    async def fetch_all(self, statement: Any, parameters: Any | None = None) -> Sequence[Any]: ...

    async def fetch_one(self, statement: Any, parameters: Any | None = None) -> Any | None: ...


class RelationalStorePort(Protocol):
    async def execute(self, statement: Any, parameters: Any | None = None) -> Any: ...

    async def fetch_all(self, statement: Any, parameters: Any | None = None) -> Sequence[Any]: ...

    async def fetch_one(self, statement: Any, parameters: Any | None = None) -> Any | None: ...

    def transaction(self) -> AbstractAsyncContextManager[Transaction]:
        """Explicit transaction scope.

        Needed because a memory commit must write memory rows, the operation log,
        and belief records atomically. Graph ingestion happens *after* this
        commit, which is what makes PostgreSQL the durability point.
        """
        ...

    async def execute_script(self, sql: str) -> None:
        """Run a multi-statement SQL script.

        Exists only for MigrationRunner. Domain services never call this — they
        depend on repository ports, not on this port at all (see the repository
        design refinement). Migration files are multi-statement DDL and cannot be
        expressed as composed Core statements.
        """
        ...

    async def health(self) -> bool: ...
