-- 0003_temporal_integrity.sql — Unit 3 schema: belief history and the operation log.
--
-- ADR-004: forward-only. Do not edit once applied.
--
-- This migration adds the second half of the bi-temporal model. Unit 2 gave every
-- fact two time axes; Unit 3 makes the *belief* axis queryable through history.
--
--     facts.valid_from / valid_to        when it was true IN THE WORLD  (already present)
--     facts.asserted_at / retracted_at   when the system CURRENTLY believes it
--     belief_history                     every belief the system has EVER held
--
-- The distinction that makes this necessary: `facts` holds only the *current* belief
-- about a statement. Answering "what did I think was true in March?" requires the
-- belief the system held in March, which may since have been corrected away. Once a
-- correction overwrites the fact row, that earlier belief is unrecoverable unless it
-- was snapshotted at the time. `belief_history` is that snapshot.
--
-- CORRECT versus SUPERSEDE — the two operations behave differently on the two axes,
-- and conflating them silently corrupts the timeline:
--
--   correct    "that's not what I said"   -> belief ENDS (retracted_at set).
--                                            World validity is untouched: the fact was
--                                            never true, so there is no period to keep.
--
--   supersede  "she moved in March"       -> belief CONTINUES (retracted_at stays NULL).
--                                            World validity ENDS (valid_to set).
--                                            We still believe the old fact was true for
--                                            its window — that is the whole point of
--                                            FR-04.4, preserving historical states.

-- ------------------------------------------------------------- belief history

CREATE TABLE IF NOT EXISTS belief_history (
    id              UUID        PRIMARY KEY,
    memory_id       UUID        NOT NULL,
    memory_kind     TEXT        NOT NULL,
    cause           TEXT        NOT NULL,

    -- The belief window this row describes.
    asserted_at     TIMESTAMPTZ NOT NULL,
    retracted_at    TIMESTAMPTZ,

    -- Snapshot of what was believed. Denormalised on purpose: a correction changes
    -- facts.statement, and without a copy here the earlier belief is lost.
    statement       TEXT        NOT NULL,
    valid_from      TIMESTAMPTZ,
    valid_to        TIMESTAMPTZ,
    superseded_by   UUID,

    reason          TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL,

    CONSTRAINT belief_history_kind_check CHECK (
        memory_kind IN ('fact', 'event', 'relationship', 'entity')
    ),
    CONSTRAINT belief_history_cause_check CHECK (
        cause IN ('asserted', 'corrected', 'superseded', 'retracted', 'source_deleted')
    ),
    CONSTRAINT belief_history_window_ordered CHECK (
        retracted_at IS NULL OR retracted_at >= asserted_at
    )
);

COMMENT ON TABLE belief_history IS
    'FR-04.8 and FR-05.5. Append-only record of every belief ever held, so that "what did I think was true at time T?" is answerable independently of what is believed now.';
COMMENT ON COLUMN belief_history.statement IS
    'Snapshotted deliberately. A correction rewrites facts.statement; without this copy the superseded belief becomes unrecoverable.';

-- The query shape for believed_at(T): rows whose belief window contains T.
CREATE INDEX IF NOT EXISTS belief_history_window_idx
    ON belief_history (asserted_at, retracted_at);

CREATE INDEX IF NOT EXISTS belief_history_memory_idx
    ON belief_history (memory_id, memory_kind, asserted_at DESC);

-- ---------------------------------------------------------- memory operations
--
-- Append-only audit of every memory mutation. Required by specification §12
-- (auditability of memory changes) and by ADR-014, which makes entity merges
-- reversible — reversal is only possible if the merge was recorded.

CREATE TABLE IF NOT EXISTS memory_operations (
    id              UUID        PRIMARY KEY,
    operation       TEXT        NOT NULL,
    memory_id       UUID,
    memory_kind     TEXT,
    entity_id       UUID,
    episode_id      UUID,
    reason          TEXT,
    detail          JSONB,
    performed_at    TIMESTAMPTZ NOT NULL,

    CONSTRAINT memory_operations_operation_check CHECK (
        operation IN (
            'commit', 'correct', 'supersede', 'retract',
            'entity_merge', 'source_delete', 'memory_delete', 'erase', 'reindex'
        )
    )
);

COMMENT ON TABLE memory_operations IS
    'Append-only. No UPDATE or DELETE path exists in OperationLogRepository — an audit trail that can be rewritten is not an audit trail.';

CREATE INDEX IF NOT EXISTS memory_operations_time_idx
    ON memory_operations (performed_at DESC);

CREATE INDEX IF NOT EXISTS memory_operations_memory_idx
    ON memory_operations (memory_id, performed_at DESC);

CREATE INDEX IF NOT EXISTS memory_operations_entity_idx
    ON memory_operations (entity_id, performed_at DESC);

-- ------------------------------------------------------- supersession linkage
--
-- `facts.superseded_by` already exists from 0002. This adds the inverse-direction
-- metadata needed to distinguish a correction from a supersession after the fact,
-- and to know when a supersession took effect in world time.

ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS corrected_from UUID REFERENCES facts (id) ON DELETE RESTRICT;

ALTER TABLE facts
    ADD COLUMN IF NOT EXISTS supersedes UUID REFERENCES facts (id) ON DELETE RESTRICT;

COMMENT ON COLUMN facts.corrected_from IS
    'Set on the replacement when this fact corrects a mistaken earlier one. Distinguishes "we recorded it wrong" from "the world changed".';
COMMENT ON COLUMN facts.supersedes IS
    'Set on the replacement when the world changed. The superseded fact keeps its belief and gains a valid_to.';

CREATE INDEX IF NOT EXISTS facts_corrected_from_idx ON facts (corrected_from);
CREATE INDEX IF NOT EXISTS facts_supersedes_idx ON facts (supersedes);
