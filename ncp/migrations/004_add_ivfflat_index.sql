-- UP
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_embedding
    ON {schema}.{prefix}chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- DOWN
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunks_embedding;
