
CREATE TABLE _job_outcomes (
    job_id            INTEGER PRIMARY KEY REFERENCES _jobs(id) ON DELETE RESTRICT,
    complexity_micros INTEGER NOT NULL CHECK (complexity_micros BETWEEN 0 AND 1000000),
    created_at        INTEGER NOT NULL
);
