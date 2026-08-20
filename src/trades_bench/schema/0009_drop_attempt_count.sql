
ALTER TABLE _invocations DROP COLUMN attempt_count;
DELETE FROM _visibility_registry WHERE "table" = '_invocations' AND "column" = 'attempt_count';
