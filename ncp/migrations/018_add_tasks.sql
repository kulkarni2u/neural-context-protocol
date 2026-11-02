-- UP
-- Task ledger (coordination primitives C, spec section 5): leased, fenced
-- tasks a router creates and workers claim. Additive -- existing deployments
-- gain an empty table and lose nothing. The ledger is opt-in
-- ([tasks].enabled), so applying this migration changes no behaviour until
-- it is switched on.
--
-- Float columns are DOUBLE PRECISION (not REAL) for the reason documented in
-- migration 014: REAL is float4 and would corrupt unix timestamps.
-- result_chunk_ids is JSONB (a bounded list of chunk ids).
CREATE TABLE IF NOT EXISTS {schema}.{prefix}tasks (
    task_id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_by TEXT NOT NULL,
    owner TEXT,
    lease_token TEXT,
    lease_expires_at DOUBLE PRECISION,
    heartbeat_at DOUBLE PRECISION,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    dedupe_key TEXT,
    parent_task_id TEXT,
    origin_whisper_id TEXT,
    checkpoint_chunk_id TEXT,
    result_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    result_turn_id TEXT,
    error TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS {prefix}tasks_pipeline_status_idx
    ON {schema}.{prefix}tasks (pipeline_id, status, created_at);
CREATE INDEX IF NOT EXISTS {prefix}tasks_owner_status_idx
    ON {schema}.{prefix}tasks (owner, status);
CREATE UNIQUE INDEX IF NOT EXISTS {prefix}tasks_dedupe_idx
    ON {schema}.{prefix}tasks (pipeline_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}tasks;
