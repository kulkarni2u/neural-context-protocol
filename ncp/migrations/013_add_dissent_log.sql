-- UP
-- CAP-T5 (dissent integrity): per-(chunk_id, identity_id) dedup so one
-- identity can't call ncp_emit_whisper(type=dissent) against the same chunk
-- unlimited times to inflate its dissent_count.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}dissent_log (
    chunk_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (chunk_id, identity_id)
);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}dissent_log;
