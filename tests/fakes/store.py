"""Fakes for RelationalStorePort and ObjectStorePort."""

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

from pca.domain.errors import SourceOfRecordUnavailable


class _FakeTransaction:
    def __init__(self, owner: "FakeRelationalStore") -> None:
        self._owner = owner

    async def execute(self, statement: Any, parameters: Any | None = None) -> Any:
        return await self._owner.execute(statement, parameters)

    async def fetch_all(self, statement: Any, parameters: Any | None = None) -> Sequence[Any]:
        return await self._owner.fetch_all(statement, parameters)

    async def fetch_one(self, statement: Any, parameters: Any | None = None) -> Any | None:
        return await self._owner.fetch_one(statement, parameters)


class FakeRelationalStore:
    """Records statements; returns scripted rows.

    `available=False` simulates PostgreSQL being down, which per constraint C-22
    must raise rather than degrade — accepting a message the system cannot
    durably store would break the product's core promise.
    """

    def __init__(self, rows: list[Any] | None = None, available: bool = True) -> None:
        self.statements: list[tuple[Any, Any]] = []
        self.scripts: list[str] = []
        self.rows = list(rows or [])
        self.committed_transactions = 0
        self._available = available

    def _guard(self) -> None:
        if not self._available:
            raise SourceOfRecordUnavailable("FakeRelationalStore is marked unavailable")

    async def execute(self, statement: Any, parameters: Any | None = None) -> Any:
        self._guard()
        self.statements.append((statement, parameters))
        return None

    async def fetch_all(self, statement: Any, parameters: Any | None = None) -> Sequence[Any]:
        self._guard()
        self.statements.append((statement, parameters))
        return list(self.rows)

    async def fetch_one(self, statement: Any, parameters: Any | None = None) -> Any | None:
        self._guard()
        self.statements.append((statement, parameters))
        return self.rows[0] if self.rows else None

    @asynccontextmanager
    async def transaction(self):
        self._guard()
        yield _FakeTransaction(self)
        self.committed_transactions += 1

    async def execute_script(self, sql: str) -> None:
        self._guard()
        self.scripts.append(sql)

    async def health(self) -> bool:
        return self._available


class FakeObjectStore:
    """Dict-backed ObjectStorePort."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self.objects
