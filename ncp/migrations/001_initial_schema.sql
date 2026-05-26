-- UP
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{prefix}chunks (
    chunk_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    scope TEXT DEFAULT 'pipeline',
    zone TEXT DEFAULT 'working',
    layer TEXT NOT NULL,
    chunk_type TEXT DEFAULT 'prose',
    content TEXT NOT NULL,
    src TEXT NOT NULL,
    written_by TEXT DEFAULT 'system',
    caused_by TEXT,
    conscious_hash TEXT,
    evidence_id TEXT,
    version INTEGER DEFAULT 1,
    supersedes TEXT,
    source_refs JSONB DEFAULT '[]'::jsonb,
    schema_version INTEGER DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL,
    base_trust DOUBLE PRECISION DEFAULT 0.7,
    generation INTEGER DEFAULT 0,
    result_confidence DOUBLE PRECISION,
    result_attempts INTEGER,
    conditions JSONB DEFAULT '[]'::jsonb,
    valid_while TEXT,
    expiry DOUBLE PRECISION,
    owner TEXT,
    meta JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}tombstones (
    chunk_id TEXT PRIMARY KEY,
    forward_ref TEXT,
    tombstoned_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}whispers (
    whisper_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    from_agent TEXT NOT NULL,
    target TEXT NOT NULL,
    whisper_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    ref TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}turn_records (
    turn_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    pipeline_id TEXT,
    task TEXT NOT NULL,
    slot TEXT NOT NULL,
    result TEXT NOT NULL,
    result_full TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}conscious_log (
    log_id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    pipeline_id TEXT,
    snapshot_hash TEXT NOT NULL,
    snapshot_json JSONB NOT NULL,
    logged_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}cost_log (
    turn_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL,
    latency_ms INTEGER,
    logged_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_pipeline
    ON {schema}.{prefix}chunks(pipeline_id, scope, zone);
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_layer
    ON {schema}.{prefix}chunks(layer);
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_created
    ON {schema}.{prefix}chunks(created_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_whispers_target
    ON {schema}.{prefix}whispers(target, expires_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_whispers_pipeline
    ON {schema}.{prefix}whispers(pipeline_id, expires_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_turns_agent
    ON {schema}.{prefix}turn_records(agent_id, pipeline_id);
CREATE INDEX IF NOT EXISTS {prefix}idx_conscious_agent
    ON {schema}.{prefix}conscious_log(agent_id, logged_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_cost_pipeline
    ON {schema}.{prefix}cost_log(pipeline_id, logged_at);

-- DOWN
DROP INDEX IF EXISTS {schema}.{prefix}idx_cost_pipeline;
DROP INDEX IF EXISTS {schema}.{prefix}idx_conscious_agent;
DROP INDEX IF EXISTS {schema}.{prefix}idx_turns_agent;
DROP INDEX IF EXISTS {schema}.{prefix}idx_whispers_pipeline;
DROP INDEX IF EXISTS {schema}.{prefix}idx_whispers_target;
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunks_created;
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunks_layer;
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunks_pipeline;

DROP TABLE IF EXISTS {schema}.{prefix}cost_log CASCADE;
DROP TABLE IF EXISTS {schema}.{prefix}conscious_log CASCADE;
DROP TABLE IF EXISTS {schema}.{prefix}turn_records CASCADE;
DROP TABLE IF EXISTS {schema}.{prefix}whispers CASCADE;
DROP TABLE IF EXISTS {schema}.{prefix}tombstones CASCADE;
DROP TABLE IF EXISTS {schema}.{prefix}chunks CASCADE;
