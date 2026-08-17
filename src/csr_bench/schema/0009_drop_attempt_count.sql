-- Migration 0009: _call_attempts is now the sole attempt counter (control.py);
-- _invocations.attempt_count is retired.
ALTER TABLE _invocations DROP COLUMN attempt_count;
DELETE FROM _visibility_registry WHERE "table" = '_invocations' AND "column" = 'attempt_count';
