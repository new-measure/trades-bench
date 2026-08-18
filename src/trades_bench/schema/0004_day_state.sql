-- Migration 0004: run-controller day and block state. Every field of every
-- table below is classified private_engine in the visibility registry; none
-- may reach a public view, SDK response, prompt, notification, predicate,
-- join, or SQL result.
--
-- Conventions: <name>_at is UTC epoch-ms INTEGER, <name>_date is TEXT
-- 'YYYY-MM-DD' Phoenix-local, and counters are plain INTEGERs — never REAL.

-- One row per service day, created at day rollover and mutated by the
-- controller as the day progresses: opening/closing instants, settlement
-- progress, and the count of appointments still short of a terminal
-- execution event.
CREATE TABLE _day_state (
    service_date                 TEXT PRIMARY KEY,
    day_type                     TEXT NOT NULL CHECK (day_type IN ('weekday','weekend')),
    opening_at                   INTEGER,
    closing_at                   INTEGER,
    settlement_status            TEXT NOT NULL DEFAULT 'pending'
                                   CHECK (settlement_status IN ('pending','settled')),
    unresolved_execution_count   INTEGER NOT NULL DEFAULT 0
                                   CHECK (unresolved_execution_count >= 0),
    last_execution_completed_at  INTEGER
);

-- One row per locked block of a service date; a block is locked exactly when
-- its row exists, and a lock never releases for the remainder of its day.
CREATE TABLE _block_locks (
    service_date TEXT NOT NULL,
    block_code   TEXT NOT NULL CHECK (block_code IN
                   ('B07_09','B09_11','B11_13','B13_15','B15_17','B17_19')),
    block_start  INTEGER NOT NULL,
    locked_at    INTEGER NOT NULL,
    PRIMARY KEY (service_date, block_code)
);

CREATE TRIGGER trg_block_locks_no_update
BEFORE UPDATE ON _block_locks
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_block_locks_no_delete
BEFORE DELETE ON _block_locks
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
