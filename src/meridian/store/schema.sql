-- Meridian's canonical schema — the readable copy.
--
-- `store/migrations/` is what actually runs; this file is what a person reads,
-- and a test applies every migration to an empty database and diffs the result
-- against this file. Without that test the two drift, and the readable copy
-- becomes a lie that is worse than no documentation at all.
--
-- Seven tables ship in the MVP. `tenant_id` columns exist everywhere and carry a
-- single fixed tenant: enforcement is deliberately absent so that adding it is
-- an additive migration rather than a rewrite.

CREATE TABLE tenant (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE suite_version (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES tenant(id),
    slug          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    adapter_spec  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT suite_version_unique UNIQUE (tenant_id, slug, version)
);

CREATE TABLE task (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenant(id),
    suite_version_id  TEXT NOT NULL REFERENCES suite_version(id) ON DELETE CASCADE,
    slug              TEXT NOT NULL,
    definition_hash   TEXT NOT NULL,
    snapshot_digest   TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'active',
    role              TEXT NOT NULL DEFAULT 'eval',
    definition        JSONB NOT NULL,
    CONSTRAINT task_unique UNIQUE (suite_version_id, slug),
    -- Both constraints carry product meaning, so they live in the database
    -- rather than in application code: application code can be bypassed and a
    -- constraint cannot.
    CONSTRAINT task_snapshot_pinned CHECK (snapshot_digest LIKE 'sha256:%'),
    CONSTRAINT task_has_outcome CHECK (jsonb_array_length(definition->'outcome_assertions') > 0),
    CONSTRAINT task_state_known CHECK (state IN ('active', 'quarantined', 'withdrawn')),
    CONSTRAINT task_role_known CHECK (role IN ('eval', 'harness_probe'))
);

CREATE TABLE task_provenance (
    task_id           TEXT PRIMARY KEY REFERENCES task(id) ON DELETE CASCADE,
    trace_id          TEXT,
    session_id        TEXT,
    failure_code      TEXT,
    synthetic_reason  TEXT,
    -- MD-FR-02, enforced where it cannot be argued with. A task whose origin
    -- nobody remembers can never be judged still-relevant, so it is never
    -- retired, so it accumulates.
    CONSTRAINT provenance_required CHECK (trace_id IS NOT NULL OR synthetic_reason IS NOT NULL)
);

CREATE TABLE run (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenant(id),
    suite_version_id  TEXT NOT NULL REFERENCES suite_version(id),
    status            TEXT NOT NULL,
    n_trials          INTEGER NOT NULL,
    k                 INTEGER NOT NULL,
    commit_sha        TEXT,
    sut_commit_sha    TEXT,
    manifest_hash     TEXT NOT NULL,
    manifest          JSONB NOT NULL,
    cost_cents        INTEGER NOT NULL DEFAULT 0,
    duration_ms       BIGINT NOT NULL DEFAULT 0,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT run_status_known
        CHECK (status IN ('complete', 'halted_budget', 'aborted')),
    CONSTRAINT run_k_fits CHECK (k >= 1 AND k <= n_trials)
);

CREATE TABLE trial (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES tenant(id),
    run_id        TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    task_slug     TEXT NOT NULL,
    trial_index   INTEGER NOT NULL,
    seed          BIGINT NOT NULL,
    outcome       TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 1,
    cassette_hash TEXT,
    turns         INTEGER NOT NULL DEFAULT 0,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   BIGINT NOT NULL DEFAULT 0,
    error_cause   TEXT,
    CONSTRAINT trial_unique UNIQUE (run_id, task_slug, trial_index),
    CONSTRAINT trial_outcome_known
        CHECK (outcome IN ('pass', 'fail', 'timeout', 'harness_error')),
    -- Only harness errors are retried, and only once. Encoding it here means a
    -- bug that retries an agent failure cannot silently write the result.
    CONSTRAINT trial_retry_policy
        CHECK (attempts = 1 OR (attempts = 2 AND outcome IN ('pass', 'fail', 'timeout',
                                                             'harness_error')))
);

CREATE TABLE score (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenant(id),
    run_id      TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    task_slug   TEXT NOT NULL,
    n           INTEGER NOT NULL,
    c           INTEGER NOT NULL,
    k           INTEGER NOT NULL,
    -- Decimal, not double precision. Probabilities that reach a hashed or
    -- compared structure must not be floats.
    pass_at_k   NUMERIC(9, 6) NOT NULL,
    pass_hat_k  NUMERIC(9, 6) NOT NULL,
    CONSTRAINT score_unique UNIQUE (run_id, task_slug),
    CONSTRAINT score_counts_sane CHECK (c >= 0 AND c <= n)
);

CREATE INDEX run_by_commit ON run (tenant_id, commit_sha);
CREATE INDEX run_by_suite ON run (suite_version_id, started_at DESC);
CREATE INDEX trial_by_run_task ON trial (run_id, task_slug);
