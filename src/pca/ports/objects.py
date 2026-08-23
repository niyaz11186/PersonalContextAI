"""ObjectStorePort — blob storage for imports, exports, and backups.

Layer L4.

Local filesystem in the MVP. The original brief listed S3-compatible storage,
but NFR-05.2 forbids infrastructure the requirements do not justify, and a
filesystem adapter behind this port satisfies FR-01.6 and FR-10 without running
a MinIO container. Swapping in S3 later means one new adapter and no domain
changes.
"""

from typing import Protocol


class ObjectStorePort(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...
