
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
