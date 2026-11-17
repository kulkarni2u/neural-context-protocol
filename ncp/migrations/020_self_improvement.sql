-- UP
-- v1.9.7 self-improvement: outcome context columns plus improvement records
-- (problems -> attempts). Additive -- old outcome rows keep NULLs (and
-- eval_source 'self_report'); the new tables start empty.
--
-- Improvement records are tables, not chunks, so decay and consolidation never
-- touch them. Float columns are DOUBLE PRECISION (see migration 014); list
-- columns are JSONB.
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS pipeline_id TEXT;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS task TEXT;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS slot TEXT;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS benchmark TEXT;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS eval_source TEXT NOT NULL DEFAULT 'self_report';
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS output_tokens INTEGER;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS input_tokens INTEGER;
ALTER TABLE {schema}.{prefix}outcomes ADD COLUMN IF NOT EXISTS turns INTEGER;

CREATE INDEX IF NOT EXISTS {prefix}idx_outcomes_task_slot
    ON {schema}.{prefix}outcomes (task, slot);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}improvement_problems (
    problem_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    opened_at DOUBLE PRECISION NOT NULL,
    alias_of TEXT
);
CREATE INDEX IF NOT EXISTS {prefix}idx_improvement_problems_scope
    ON {schema}.{prefix}improvement_problems (pipeline_id, scope);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}improvement_attempts (
    attempt_id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    accepted INTEGER NOT NULL DEFAULT 0,
    what TEXT NOT NULL,
    rationale TEXT,
    files JSONB NOT NULL DEFAULT '[]'::jsonb,
    candidate_chunk_id TEXT,
    predicted JSONB NOT NULL DEFAULT '[]'::jsonb,
    fixed JSONB NOT NULL DEFAULT '[]'::jsonb,
    missed JSONB NOT NULL DEFAULT '[]'::jsonb,
    passed DOUBLE PRECISION,
    total DOUBLE PRECISION,
    mean_output_tokens DOUBLE PRECISION,
    anchor_rate DOUBLE PRECISION,
    anchor_output_tokens DOUBLE PRECISION,
    model TEXT,
    benchmark TEXT,
    note TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS {prefix}idx_improvement_attempts_problem
    ON {schema}.{prefix}improvement_attempts (problem_id);
CREATE INDEX IF NOT EXISTS {prefix}idx_improvement_attempts_candidate
    ON {schema}.{prefix}improvement_attempts (candidate_chunk_id);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}improvement_attempts;
DROP TABLE IF EXISTS {schema}.{prefix}improvement_problems;
DROP INDEX IF EXISTS {schema}.{prefix}idx_outcomes_task_slot;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS turns;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS input_tokens;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS output_tokens;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS eval_source;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS benchmark;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS model;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS slot;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS task;
ALTER TABLE {schema}.{prefix}outcomes DROP COLUMN IF EXISTS pipeline_id;
