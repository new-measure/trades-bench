-- Migration 0008: job outcomes. _job_outcomes carries the hidden complexity
-- draw made once at job creation (job_id PRIMARY KEY, one row per job).
-- Child table like _cancellation_schedule: no origin column. Not append-only
-- (a later migration extends/updates this row), so no immutability triggers.
CREATE TABLE _job_outcomes (
    job_id            INTEGER PRIMARY KEY REFERENCES _jobs(id) ON DELETE RESTRICT,
    complexity_micros INTEGER NOT NULL CHECK (complexity_micros BETWEEN 0 AND 1000000),
    created_at        INTEGER NOT NULL
);
