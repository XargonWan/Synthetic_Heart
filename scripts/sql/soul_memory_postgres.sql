-- SOUL memory foundation schema (PostgreSQL + pgvector)
-- v1: phase-1 baseline from SOUL-REWRITE-TASK.md
--
-- EMBEDDING_DIM placeholder: the setup wizard substitutes {EMBEDDING_DIM} with
-- the dimension of the chosen embedder model (e.g. 768, 384, 3072) before
-- applying this file.  Do not hard-code a dimension here.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS mem_cells (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    episodic_trace TEXT NOT NULL,
    atomic_facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    emotional_tag JSONB NOT NULL,
    foresight_signals JSONB NOT NULL DEFAULT '[]'::jsonb,
    timestamp TIMESTAMPTZ NOT NULL,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    explicit_importance REAL NOT NULL DEFAULT 0,
    consolidated BOOLEAN NOT NULL DEFAULT FALSE,
    scene_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mem_cell_vectors (
    mem_cell_id TEXT PRIMARY KEY REFERENCES mem_cells(id) ON DELETE CASCADE,
    embedding VECTOR({EMBEDDING_DIM}) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mem_cells_timestamp ON mem_cells (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_mem_cells_session_id ON mem_cells (session_id);
CREATE INDEX IF NOT EXISTS idx_mem_cells_consolidated ON mem_cells (consolidated);
CREATE INDEX IF NOT EXISTS idx_mem_cells_atomic_facts_gin ON mem_cells USING gin (atomic_facts);
CREATE INDEX IF NOT EXISTS idx_mem_cells_episodic_trace_tsv
    ON mem_cells USING gin (to_tsvector('simple', episodic_trace));
CREATE INDEX IF NOT EXISTS idx_mem_cells_episodic_trace_trgm
    ON mem_cells USING gin (episodic_trace gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_mem_cell_vectors_hnsw
    ON mem_cell_vectors USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS mem_scenes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    cell_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS kg_triples (
    id BIGSERIAL PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    scene_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_kg_triples_subject ON kg_triples(subject);
CREATE INDEX IF NOT EXISTS idx_kg_triples_predicate ON kg_triples(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_triples_temporal ON kg_triples(valid_from, valid_until);

CREATE TABLE IF NOT EXISTS foresight_signals (
    id BIGSERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    valid_until DATE NOT NULL,
    trigger TEXT NOT NULL,
    emotional_implication JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_cell_id TEXT,
    priority REAL NOT NULL DEFAULT 0.5,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_foresight_active
    ON foresight_signals(valid_until, archived)
    WHERE archived = FALSE;

CREATE TABLE IF NOT EXISTS dsp_extractions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL,
    user_facts JSONB NOT NULL DEFAULT '[]'::jsonb,
    user_preferences JSONB NOT NULL DEFAULT '[]'::jsonb,
    ai_self_facts JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS dsp_versions (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_dsp_versions_active
    ON dsp_versions(active)
    WHERE active = TRUE;

CREATE TABLE IF NOT EXISTS soul_emotion_snapshots (
    id BIGSERIAL PRIMARY KEY,
    joy REAL NOT NULL,
    fear REAL NOT NULL,
    sad REAL NOT NULL,
    anger REAL NOT NULL,
    source TEXT NOT NULL,
    context TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS soul_metrics (
    metric_key TEXT NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(metric_key, measured_at)
);
