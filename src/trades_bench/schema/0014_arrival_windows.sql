PRAGMA legacy_alter_table = ON;

UPDATE _job_types SET scheduled_blocks = 1 WHERE code = 'hvac_installation';

ALTER TABLE _technician_block_holds RENAME TO _technician_block_holds_previous;
CREATE TABLE _technician_block_holds (
    technician_id           INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    block_start             INTEGER NOT NULL,
    hold_kind               TEXT NOT NULL CHECK (hold_kind IN ('job','training','absence')),
    appointment_id          INTEGER REFERENCES _appointments(id) ON DELETE RESTRICT,
    non_job_appointment_id  INTEGER REFERENCES _non_job_appointments(id) ON DELETE RESTRICT,
    created_at              INTEGER NOT NULL,
    CHECK ((appointment_id IS NULL) != (non_job_appointment_id IS NULL))
);
INSERT INTO _technician_block_holds
    (technician_id, block_start, hold_kind, appointment_id, non_job_appointment_id, created_at)
SELECT technician_id, block_start, hold_kind, appointment_id, non_job_appointment_id, created_at
FROM _technician_block_holds_previous;
DROP TABLE _technician_block_holds_previous;
CREATE UNIQUE INDEX _technician_block_holds_job
    ON _technician_block_holds (appointment_id, block_start) WHERE hold_kind = 'job';
CREATE UNIQUE INDEX _technician_block_holds_non_job
    ON _technician_block_holds (technician_id, block_start) WHERE hold_kind != 'job';
CREATE INDEX _technician_block_holds_slot
    ON _technician_block_holds (technician_id, block_start);

PRAGMA legacy_alter_table = OFF;
