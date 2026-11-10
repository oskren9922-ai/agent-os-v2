-- Stage One: the function registry and the invocation log.
-- Governance columns (permissions, side_effect, requires_confirmation,
-- risk_level) exist from day one; migration 0002 adds what enforces them.

CREATE TABLE adapters (
    name            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    schema_hash     TEXT NOT NULL,
    function_count  INTEGER NOT NULL DEFAULT 0,
    registered_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE functions (
    name                    TEXT PRIMARY KEY,
    adapter                 TEXT NOT NULL REFERENCES adapters(name) ON DELETE CASCADE,
    description             TEXT NOT NULL,
    input_schema            TEXT NOT NULL,
    output_schema           TEXT,
    permissions             TEXT NOT NULL DEFAULT '[]',
    side_effect             TEXT NOT NULL DEFAULT 'none',
    requires_confirmation   INTEGER NOT NULL DEFAULT 0,
    risk_level              TEXT NOT NULL DEFAULT 'read',
    metadata                TEXT NOT NULL DEFAULT '{}',
    registered_at           TEXT NOT NULL
);
CREATE INDEX idx_functions_adapter ON functions(adapter);

CREATE TABLE invocations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    function     TEXT NOT NULL,
    adapter      TEXT,
    actor        TEXT NOT NULL,
    args         TEXT NOT NULL,
    status       TEXT NOT NULL,
    result       TEXT,
    error        TEXT,
    duration_ms  REAL,
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_invocations_function ON invocations(function);
