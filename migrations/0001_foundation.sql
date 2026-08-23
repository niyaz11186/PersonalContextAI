-- 0001_foundation.sql — Unit 1 schema.
--
-- ADR-004: raw SQL, forward-only, numbered. No Alembic. A mistake here is
-- corrected by a later migration, never by editing this file; MigrationRunner
-- verifies checksums of already-applied files and refuses to start on drift.
--
-- ADR-005: PostgreSQL is the system of record. Everything below is authoritative.
-- Neo4j holds a rebuildable projection and is never the source of truth.
--
-- ADR-011: every instant is `timestamptz` (a UTC instant). Bare `timestamp` is
-- never used. A companion `zone` column stores the IANA zone active at capture,
-- per record rather than globally, so that history stays correct if the user
-- relocates. This is what lets "last Tuesday" resolve against the right local
-- day boundary years later.
--
-- NOTE: no BEGIN/COMMIT here. MigrationRunner wraps each file in its own
-- transaction; an explicit block inside the file would nest and confuse the
-- driver's transaction state.

-- ---------------------------------------------------------------- migrations

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------- conversations

CREATE TABLE IF NOT EXISTS conversations (
    id          UUID        PRIMARY KEY,
    title       TEXT,
    started_at  TIMESTAMPTZ NOT NULL,
    zone        TEXT        NOT NULL,
    deleted_at  TIMESTAMPTZ,
    delete_reason TEXT
);

COMMENT ON COLUMN conversations.deleted_at IS
    'Tombstone for source deletion (ADR-012). Never a hard delete except via the explicit erase path.';

CREATE INDEX IF NOT EXISTS conversations_started_at_idx
    ON conversations (started_at DESC);

-- ------------------------------------------------------------------ messages
-- Append-only (FR-01.4). No UPDATE or DELETE path exists in ConversationService.
-- The immutability is enforced by the absence of those operations rather than by
-- a trigger, because the erase path (NFR-01.6) must still be able to purge.

CREATE TABLE IF NOT EXISTS messages (
    id              UUID        PRIMARY KEY,
    conversation_id UUID        NOT NULL REFERENCES conversations (id) ON DELETE RESTRICT,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT        NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL,
    zone            TEXT        NOT NULL,
    seq             BIGSERIAL   NOT NULL
);

COMMENT ON COLUMN messages.captured_at IS
    'Resolution anchor for every relative time reference in this message (ADR-010).';
COMMENT ON COLUMN messages.seq IS
    'Monotonic ordering within a conversation. captured_at alone is insufficient because two messages can share an instant.';

CREATE INDEX IF NOT EXISTS messages_conversation_seq_idx
    ON messages (conversation_id, seq);

-- ON DELETE RESTRICT above is deliberate: deleting a conversation must go
-- through DeletionService so the corroboration rule runs (ADR-012). A cascade
-- would silently orphan or destroy derived memory.

-- ------------------------------------------------------------------ episodes
-- The replay source. ADR-005 requires the exact payload sent to the graph to be
-- persisted here, which is what makes a rebuild byte-faithful and re-extraction
-- with a better model possible later.

CREATE TABLE IF NOT EXISTS episodes (
    id              UUID        PRIMARY KEY,
    content         TEXT        NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    zone            TEXT        NOT NULL,
    conversation_id UUID        REFERENCES conversations (id) ON DELETE RESTRICT,
    message_id      UUID        REFERENCES messages (id) ON DELETE RESTRICT,
    document_id     UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_at     TIMESTAMPTZ,
    llm_model       TEXT,
    embedding_model TEXT,
    CONSTRAINT episodes_has_a_source CHECK (
        conversation_id IS NOT NULL OR document_id IS NOT NULL
    )
);

COMMENT ON COLUMN episodes.ingested_at IS
    'NULL until the graph accepted it. Doubles as the replay watermark for resumable reindex.';
COMMENT ON COLUMN episodes.embedding_model IS
    'Recorded because embeddings from different models are not comparable. Without this the mismatch is undetectable after a model change (ADR-013).';

CREATE INDEX IF NOT EXISTS episodes_pending_idx
    ON episodes (created_at)
    WHERE ingested_at IS NULL;

CREATE INDEX IF NOT EXISTS episodes_occurred_at_idx
    ON episodes (occurred_at);

-- -------------------------------------------------------- workflow checkpoints
-- LangGraph durable state (ADR-006). Backed by the PostgreSQL instance already
-- in the stack rather than adding infrastructure (NFR-05.2). This is what makes
-- the clarification workflow resumable across a process restart.

CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    thread_id   TEXT        NOT NULL,
    checkpoint_id TEXT      NOT NULL,
    parent_id   TEXT,
    workflow    TEXT        NOT NULL,
    state       JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS workflow_checkpoints_thread_idx
    ON workflow_checkpoints (thread_id, created_at DESC);
