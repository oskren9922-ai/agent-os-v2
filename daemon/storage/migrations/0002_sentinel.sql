-- Sentinel: webhook events, the job queue, agent decisions, the approval
-- queue, the append-only audit log and per-client payment profiles.

CREATE TABLE events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    paypal_event_id  TEXT NOT NULL UNIQUE,      -- duplicates from PayPal retries are dropped
    event_type       TEXT NOT NULL,
    topic            TEXT NOT NULL,
    resource_type    TEXT,
    resource_id      TEXT,
    summary          TEXT,
    payload          TEXT NOT NULL,
    verified         INTEGER NOT NULL DEFAULT 0,
    simulated        INTEGER NOT NULL DEFAULT 0,
    received_at      TEXT NOT NULL
);
CREATE INDEX idx_events_topic ON events(topic);

CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,                -- 'event' | 'collections.sweep'
    event_id      INTEGER REFERENCES events(id),
    payload       TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    run_after     TEXT NOT NULL,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_jobs_status ON jobs(status, run_after);

CREATE TABLE decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    agent              TEXT NOT NULL,
    event_id           INTEGER REFERENCES events(id),
    job_id             INTEGER,
    run_id             TEXT,                    -- ties the agent's earlier read calls to this decision
    subject_type       TEXT NOT NULL,           -- dispute | invoice | subscription
    subject_id         TEXT NOT NULL,
    client_id          TEXT,
    client_name        TEXT,
    input_summary      TEXT,
    action             TEXT NOT NULL,           -- fight | offer | accept | reminder | discount | ...
    summary            TEXT,
    reasoning          TEXT,
    confidence         REAL,
    decision           TEXT NOT NULL,           -- the full decision JSON
    tool_calls         TEXT NOT NULL DEFAULT '[]',
    model              TEXT,
    status             TEXT NOT NULL DEFAULT 'decided',
    proposed_function  TEXT,
    pending_action_id  INTEGER,
    amount             TEXT,
    currency           TEXT,
    result             TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX idx_decisions_subject ON decisions(subject_type, subject_id);

CREATE TABLE pending_actions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    function         TEXT NOT NULL,
    args             TEXT NOT NULL,
    tier             TEXT NOT NULL,
    amount           TEXT,
    currency         TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | executed | failed
    idempotency_key  TEXT NOT NULL UNIQUE,
    decision_id      INTEGER,
    client_id        TEXT,
    actor            TEXT NOT NULL,
    reason           TEXT,
    result           TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    decided_at       TEXT,
    decided_by       TEXT,
    executed_at      TEXT
);
CREATE INDEX idx_pending_status ON pending_actions(status);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    function   TEXT,
    args_hash  TEXT,
    status     TEXT,
    detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_audit_function ON audit_log(function);

-- Append-only: the database itself refuses edits and deletes.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE client_profiles (
    client_id           TEXT PRIMARY KEY,       -- the payer's email
    name                TEXT,
    invoices_count      INTEGER NOT NULL DEFAULT 0,
    paid_count          INTEGER NOT NULL DEFAULT 0,
    avg_days_late       REAL NOT NULL DEFAULT 0,
    on_time_share       REAL NOT NULL DEFAULT 0,
    outstanding_amount  TEXT,
    currency            TEXT,
    last_updated        TEXT NOT NULL
);

-- One row per automatic message, used for the per-client rate limit.
CREATE TABLE rate_events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    key  TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX idx_rate_events_key ON rate_events(key, ts);
