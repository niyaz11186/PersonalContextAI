-- 0004_extraction_status.sql — Unit 5 schema: the extraction barrier and real
-- checkpoint storage.
--
-- ADR-004: forward-only. Do not edit once applied.
--
-- Two unrelated-looking changes belong in one migration because both exist to move
-- work off the response path, which is Unit 5's whole purpose:
--
--     extraction_status           makes background extraction durable and ordered
--     workflow_checkpoints        makes an interrupted workflow resumable
--
-- ------------------------------------------------------------ extraction status
--
-- ADR-008 requires extraction state to be DURABLE, not merely an in-process lock:
-- "if the process dies mid-extraction, the pending episode must be recoverable on
-- restart". These rows are that record. The in-process asyncio lock in
-- ExtractionCoordinator is an optimisation layered on top of this table, never a
-- substitute for it.
--
-- The primary key is episode_id rather than a synthetic id. That is the whole
-- idempotency guarantee ADR-008 asks for: a retried submit of the same episode
-- collides at the database instead of racing in the process, so extraction cannot
-- double-write facts. A synthetic key would permit two rows for one episode and
-- turn a crash-recovery retry into duplicated memory.

CREATE TABLE IF NOT EXISTS extraction_status (
    episode_id      UUID        PRIMARY KEY
                                REFERENCES episodes(id) ON DELETE CASCADE,

    -- NULL for imported documents (Unit 7): they have no conversation whose
    -- message order the barrier must preserve.
    conversation_id UUID        REFERENCES conversations(id) ON DELETE RESTRICT,

    state           TEXT        NOT NULL,

    -- Distinguishes "failed once, will retry" from "failed repeatedly". Without a
    -- count, recover_pending cannot tell a transient Gemini outage from an episode
    -- that fails deterministically and would be retried forever at every startup.
    attempts        INTEGER     NOT NULL DEFAULT 0,

    submitted_at    TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,

    -- Truncated by the writer. Retained because an extraction that failed silently
    -- is indistinguishable from one that found nothing worth remembering.
    error           TEXT,

    updated_at      TIMESTAMPTZ NOT NULL,

    CONSTRAINT extraction_status_state_check CHECK (
        state IN ('pending', 'running', 'succeeded', 'failed', 'abandoned')
    ),
    CONSTRAINT extraction_status_finished_ordered CHECK (
        finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at
    )
);

COMMENT ON TABLE extraction_status IS
    'ADR-008. Durable per-episode extraction state backing the per-conversation write barrier. Primary key is episode_id so that a retried submit is idempotent at the database rather than racing in the process.';
COMMENT ON COLUMN extraction_status.state IS
    'abandoned means the barrier timed out waiting and the reader proceeded with a degradation notice (NFR-06.5). The work itself stays recoverable.';

-- The barrier's only hot query: "is anything still in flight for this conversation?"
-- Partial, because finished rows accumulate forever and are never part of that
-- answer — a full index would grow without bound to serve a lookup that only ever
-- cares about a handful of live rows.
CREATE INDEX IF NOT EXISTS extraction_status_barrier_idx
    ON extraction_status (conversation_id)
    WHERE state IN ('pending', 'running');

-- recover_pending at startup: oldest unfinished work first.
CREATE INDEX IF NOT EXISTS extraction_status_recovery_idx
    ON extraction_status (state, submitted_at);

-- -------------------------------------------------------- workflow checkpoints
--
-- The table authored in 0001_foundation.sql cannot hold a LangGraph 1.2 checkpoint.
-- Verified against the installed langgraph==1.2.11 rather than its documentation:
--
--   * No checkpoint_ns column. Namespaces distinguish a subgraph's checkpoints from
--     its parent's; without one they collide on the primary key and a nested
--     workflow silently overwrites its parent's state.
--   * No metadata column. BaseCheckpointSaver.aput receives metadata separately from
--     the checkpoint and alist filters on it.
--   * No companion table for aput_writes. Pending writes are how LangGraph resumes a
--     task that was interrupted mid-execution; discarding them loses exactly the
--     state a resume needs.
--   * state JSONB is the wrong type. The serde emits (type: str, payload: bytes),
--     and bytes are not JSON.
--
-- Restructuring rather than adding a table keeps one checkpoint store. Safe because
-- the table has never held a row: Unit 1b compiled its graph with no checkpointer
-- attached, which is why the mismatch went unnoticed for four units.
--
-- langgraph-checkpoint-postgres was rejected: it requires psycopg, which would put a
-- second PostgreSQL driver and connection pool beside the existing asyncpg one.

-- ADR-004 gives migrations no downgrade path, and no backup exists until Unit 7, so
-- a DROP COLUMN that turned out to be wrong would be unrecoverable. Assert the
-- premise instead of trusting it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM workflow_checkpoints) THEN
        RAISE EXCEPTION
            'workflow_checkpoints is not empty; 0004 assumes it has never been written to. Back up before proceeding and migrate the rows deliberately.';
    END IF;
END $$;

ALTER TABLE workflow_checkpoints DROP CONSTRAINT IF EXISTS workflow_checkpoints_pkey;
ALTER TABLE workflow_checkpoints DROP COLUMN IF EXISTS state;

ALTER TABLE workflow_checkpoints
    ADD COLUMN IF NOT EXISTS checkpoint_ns TEXT NOT NULL DEFAULT '';
ALTER TABLE workflow_checkpoints
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE workflow_checkpoints
    ADD COLUMN IF NOT EXISTS type TEXT;
ALTER TABLE workflow_checkpoints
    ADD COLUMN IF NOT EXISTS payload BYTEA;

-- Relaxed from NOT NULL. We populate it from our own graph config where we control
-- it, but LangGraph creates checkpoints for nested graphs whose config we do not
-- author, and failing a checkpoint write over a missing label would trade durable
-- state for a cosmetic column.
ALTER TABLE workflow_checkpoints ALTER COLUMN workflow DROP NOT NULL;

ALTER TABLE workflow_checkpoints
    ADD CONSTRAINT workflow_checkpoints_pkey
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);

COMMENT ON TABLE workflow_checkpoints IS
    'ADR-006. LangGraph durable state on the PostgreSQL already in the stack (NFR-05.2). This is what lets ClarificationWorkflow interrupt, survive a process restart, and resume with intact state.';
COMMENT ON COLUMN workflow_checkpoints.checkpoint_ns IS
    'Subgraph namespace. Empty string for the root graph. Part of the primary key because without it a nested graph overwrites its parent.';
COMMENT ON COLUMN workflow_checkpoints.payload IS
    'BYTEA, not JSONB: the LangGraph serde emits (type, bytes) and bytes are not JSON.';

-- Pending writes for tasks interrupted mid-execution (BaseCheckpointSaver.aput_writes).
-- idx preserves emission order within a task; the channel is which state key the
-- write targets.
CREATE TABLE IF NOT EXISTS workflow_checkpoint_writes (
    thread_id     TEXT        NOT NULL,
    checkpoint_ns TEXT        NOT NULL DEFAULT '',
    checkpoint_id TEXT        NOT NULL,
    task_id       TEXT        NOT NULL,
    idx           INTEGER     NOT NULL,
    channel       TEXT        NOT NULL,
    type          TEXT,
    payload       BYTEA,
    task_path     TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

COMMENT ON TABLE workflow_checkpoint_writes IS
    'Writes emitted by a task before it was interrupted. Resuming without these loses the partial progress the checkpoint was taken to preserve.';

-- Loading a checkpoint reads every write for it; deleting a thread deletes them all.
CREATE INDEX IF NOT EXISTS workflow_checkpoint_writes_thread_idx
    ON workflow_checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id);
