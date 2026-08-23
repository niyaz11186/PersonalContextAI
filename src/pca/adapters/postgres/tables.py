"""SQLAlchemy Core table metadata — for QUERY BUILDING ONLY.

Layer L5.

ADR-009 boundary rule, restated because it is easy to violate by habit:

    These Table objects are NOT the schema source of truth. The numbered `.sql`
    files in `migrations/` are authoritative. `metadata.create_all()` is never
    called anywhere in this codebase.

They exist so that queries can be composed safely and conditionally — hybrid
retrieval builds different predicates per request, and building that by string
concatenation is where both SQL injection and unreadable code come from.

`SchemaDriftCheck` compares these declarations against the live schema at startup
and fails loudly on mismatch, which recovers the main safety property given up by
not using Alembic.

Every timestamp column is `TIMESTAMP(timezone=True)` — a UTC instant — with a
companion `zone` column holding the IANA zone active at capture (ADR-011).
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Text,
    TIMESTAMP,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Text, primary_key=True),
    Column("checksum", Text, nullable=False),
    Column("applied_at", TIMESTAMP(timezone=True), nullable=False),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("title", Text, nullable=True),
    Column("started_at", TIMESTAMP(timezone=True), nullable=False),
    Column("zone", Text, nullable=False),
    Column("deleted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("delete_reason", Text, nullable=True),
    Index("conversations_started_at_idx", "started_at"),
)

messages = Table(
    "messages",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("role", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("captured_at", TIMESTAMP(timezone=True), nullable=False),
    Column("zone", Text, nullable=False),
    # Monotonic ordering. captured_at alone is insufficient because two messages
    # can share an instant, making timestamp ordering non-deterministic.
    Column("seq", BigInteger, nullable=False),
    CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
    Index("messages_conversation_seq_idx", "conversation_id", "seq"),
)

episodes = Table(
    "episodes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("content", Text, nullable=False),
    Column("occurred_at", TIMESTAMP(timezone=True), nullable=False),
    Column("zone", Text, nullable=False),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "message_id",
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("document_id", UUID(as_uuid=True), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    # NULL until the graph accepted it. Doubles as the replay watermark.
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=True),
    Column("llm_model", Text, nullable=True),
    # Recorded because embeddings from different models are not comparable.
    # Without this the mismatch is undetectable after a model change (ADR-013).
    Column("embedding_model", Text, nullable=True),
    Index("episodes_occurred_at_idx", "occurred_at"),
)

workflow_checkpoints = Table(
    "workflow_checkpoints",
    metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_id", Text, primary_key=True),
    Column("parent_id", Text, nullable=True),
    Column("workflow", Text, nullable=False),
    Column("state", JSONB, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
)

__all__ = [
    "conversations",
    "episodes",
    "messages",
    "metadata",
    "schema_migrations",
    "workflow_checkpoints",
]
