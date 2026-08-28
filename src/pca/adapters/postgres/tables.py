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
    Boolean,
    CheckConstraint,
    Column,
    Float,
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


# ===========================================================================
# Unit 2 — memory model. Mirrors 0002_memory_model.sql.
#
# Same rule as above: these are for query building only. The .sql files are the
# schema authority and create_all() is never called.
# ===========================================================================

entities = Table(
    "entities",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("is_provisional", Boolean, nullable=False, default=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("merged_into", UUID(as_uuid=True), ForeignKey("entities.id"), nullable=True),
    Column("merged_at", TIMESTAMP(timezone=True), nullable=True),
    Column("merge_reason", Text, nullable=True),
    Column("deleted_at", TIMESTAMP(timezone=True), nullable=True),
)

entity_aliases = Table(
    "entity_aliases",
    metadata,
    Column(
        "entity_id",
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("alias", Text, primary_key=True),
)

facts = Table(
    "facts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("statement", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("confidence", Text, nullable=False),
    Column("salience", Float, nullable=False, default=0.0),
    Column("salience_category", Text, nullable=True),
    # world time — when the fact was true
    Column("valid_from", TIMESTAMP(timezone=True), nullable=True),
    Column("valid_to", TIMESTAMP(timezone=True), nullable=True),
    # belief time — when the system believed it. A separate axis (ADR-011).
    Column("asserted_at", TIMESTAMP(timezone=True), nullable=False),
    Column("retracted_at", TIMESTAMP(timezone=True), nullable=True),
    # the original phrase, never discarded (ADR-010)
    Column("temporal_raw_phrase", Text, nullable=True),
    Column("temporal_granularity", Text, nullable=True),
    Column("temporal_method", Text, nullable=True),
    Column("temporal_anchor_zone", Text, nullable=True),
    Column("superseded_by", UUID(as_uuid=True), ForeignKey("facts.id"), nullable=True),
    # 0003. Distinguishes the two ways a fact can be replaced. Without both columns
    # the reason for a replacement is unrecoverable after the fact, and "we recorded
    # it wrong" reads identically to "the world changed".
    Column("corrected_from", UUID(as_uuid=True), ForeignKey("facts.id"), nullable=True),
    Column("supersedes", UUID(as_uuid=True), ForeignKey("facts.id"), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
)

fact_subjects = Table(
    "fact_subjects",
    metadata,
    Column(
        "fact_id",
        UUID(as_uuid=True),
        ForeignKey("facts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("entity_id", UUID(as_uuid=True), ForeignKey("entities.id"), primary_key=True),
)

events = Table(
    "events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("description", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("salience", Float, nullable=False, default=0.0),
    Column("salience_category", Text, nullable=True),
    Column("occurred_at", TIMESTAMP(timezone=True), nullable=True),
    Column("occurred_through", TIMESTAMP(timezone=True), nullable=True),
    Column("temporal_raw_phrase", Text, nullable=True),
    Column("temporal_granularity", Text, nullable=True),
    Column("temporal_method", Text, nullable=True),
    Column("temporal_anchor_zone", Text, nullable=True),
    Column("retracted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
)

event_participants = Table(
    "event_participants",
    metadata,
    Column(
        "event_id",
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("entity_id", UUID(as_uuid=True), ForeignKey("entities.id"), primary_key=True),
)

relationships = Table(
    "relationships",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("from_entity_id", UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False),
    Column("to_entity_id", UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False),
    Column("relation_type", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("valid_from", TIMESTAMP(timezone=True), nullable=True),
    Column("valid_to", TIMESTAMP(timezone=True), nullable=True),
    Column("retracted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
)

# Many-to-many by design. ADR-012's corroboration rule needs to count how many
# sources still support a memory before deciding whether deleting one should
# retract it.
provenance_index = Table(
    "provenance_index",
    metadata,
    Column("memory_id", UUID(as_uuid=True), primary_key=True),
    Column("memory_kind", Text, primary_key=True),
    Column(
        "episode_id",
        UUID(as_uuid=True),
        ForeignKey("episodes.id"),
        primary_key=True,
    ),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True),
    Column("message_id", UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True),
    Column("document_id", UUID(as_uuid=True), nullable=True),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False),
)

__all__ += [  # noqa: PLE0605 - extending the list defined above
    "entities",
    "entity_aliases",
    "event_participants",
    "events",
    "fact_subjects",
    "facts",
    "provenance_index",
    "relationships",
]

# --------------------------------------------------------------- 0003, Unit 3

# Append-only. `statement`, `valid_from`, and `valid_to` are snapshots rather than
# references: a correction rewrites facts.statement, so a foreign key here would
# resolve to the corrected text and the earlier belief would be lost — defeating the
# only purpose this table has.
belief_history = Table(
    "belief_history",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("memory_id", UUID(as_uuid=True), nullable=False),
    Column("memory_kind", Text, nullable=False),
    Column("cause", Text, nullable=False),
    Column("asserted_at", TIMESTAMP(timezone=True), nullable=False),
    Column("retracted_at", TIMESTAMP(timezone=True), nullable=True),
    Column("statement", Text, nullable=False),
    Column("valid_from", TIMESTAMP(timezone=True), nullable=True),
    Column("valid_to", TIMESTAMP(timezone=True), nullable=True),
    Column("superseded_by", UUID(as_uuid=True), nullable=True),
    Column("reason", Text, nullable=True),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(
        "memory_kind IN ('fact', 'event', 'relationship', 'entity')",
        name="belief_history_kind_check",
    ),
    CheckConstraint(
        "cause IN ('asserted', 'corrected', 'superseded', 'retracted', 'source_deleted')",
        name="belief_history_cause_check",
    ),
    CheckConstraint(
        "retracted_at IS NULL OR retracted_at >= asserted_at",
        name="belief_history_window_ordered",
    ),
    Index("belief_history_window_idx", "asserted_at", "retracted_at"),
    Index("belief_history_memory_idx", "memory_id", "memory_kind", "asserted_at"),
)

memory_operations = Table(
    "memory_operations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("operation", Text, nullable=False),
    Column("memory_id", UUID(as_uuid=True), nullable=True),
    Column("memory_kind", Text, nullable=True),
    Column("entity_id", UUID(as_uuid=True), nullable=True),
    Column("episode_id", UUID(as_uuid=True), nullable=True),
    Column("reason", Text, nullable=True),
    Column("detail", JSONB, nullable=True),
    Column("performed_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(
        "operation IN ('commit', 'correct', 'supersede', 'retract', "
        "'entity_merge', 'source_delete', 'memory_delete', 'erase', 'reindex')",
        name="memory_operations_operation_check",
    ),
    Index("memory_operations_time_idx", "performed_at"),
    Index("memory_operations_memory_idx", "memory_id"),
    Index("memory_operations_entity_idx", "entity_id"),
)

__all__ += [  # noqa: PLE0605
    "belief_history",
    "memory_operations",
]
