-- MedAnon Application State Schema
-- Runs on the dedicated app-db PostgreSQL instance.
-- All tables live in the "medanon" schema.

CREATE SCHEMA IF NOT EXISTS medanon;

-- -------------------------------------------------------------------
-- Jobs (replaces SQLite job store)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS medanon.jobs (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    params          JSONB NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    result_path     TEXT,
    error           TEXT,
    checkpoint_data JSONB
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON medanon.jobs (status, created_at);

CREATE INDEX IF NOT EXISTS idx_jobs_type_created
    ON medanon.jobs (type, created_at);

-- -------------------------------------------------------------------
-- Subscriptions (replaces SQLite subscription store)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS medanon.subscriptions (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    criteria     TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    headers      TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    resource     TEXT NOT NULL
);

-- -------------------------------------------------------------------
-- Config metadata index (replaces SQLite config store)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS medanon.configs (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    is_system   BOOLEAN NOT NULL DEFAULT FALSE
);

-- -------------------------------------------------------------------
-- Staged resources (already existed in staging store DDL)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS medanon.staged_resources (
    id            BIGSERIAL PRIMARY KEY,
    job_id        TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_json JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,
    CONSTRAINT uq_job_resource UNIQUE (job_id, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_staged_job_status
    ON medanon.staged_resources (job_id, status);

CREATE INDEX IF NOT EXISTS idx_staged_expires
    ON medanon.staged_resources (expires_at)
    WHERE expires_at IS NOT NULL;

-- -------------------------------------------------------------------
-- Processing runs (scoring persistence for all processing paths)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS medanon.processing_runs (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    config_profile  TEXT NOT NULL DEFAULT 'auto',
    resource_count  INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    input_type      TEXT NOT NULL DEFAULT '',
    summary         JSONB,
    score           JSONB
);

CREATE INDEX IF NOT EXISTS idx_processing_runs_created
    ON medanon.processing_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_processing_runs_endpoint
    ON medanon.processing_runs (endpoint, created_at DESC);

-- -------------------------------------------------------------------
-- Job details (frontend parse cache — resource counts, field analysis)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS medanon.job_details (
    job_id     TEXT PRIMARY KEY,
    detail     JSONB NOT NULL,
    created_at TEXT NOT NULL
);
