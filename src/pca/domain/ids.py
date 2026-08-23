"""Typed identifiers.

Layer L0. Standard library only.

These are NewTypes rather than bare UUIDs so that passing a MessageId where an
EpisodeId is expected is a type error rather than a silent bug. The distinction
matters most in provenance code, where several id kinds travel together.
"""

from typing import NewType
from uuid import UUID

ConversationId = NewType("ConversationId", UUID)
MessageId = NewType("MessageId", UUID)
EpisodeId = NewType("EpisodeId", UUID)
DocumentId = NewType("DocumentId", UUID)
MemoryId = NewType("MemoryId", UUID)
EntityId = NewType("EntityId", UUID)
OperationId = NewType("OperationId", UUID)
