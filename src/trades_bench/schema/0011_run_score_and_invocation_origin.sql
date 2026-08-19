ALTER TABLE _run ADD COLUMN final_score_cents INTEGER;
ALTER TABLE _run ADD COLUMN track TEXT;
ALTER TABLE _run ADD COLUMN attempt_definition TEXT;
ALTER TABLE _invocations ADD COLUMN origin_event_id INTEGER REFERENCES _events(event_id);
WITH ranked_invocations AS (
    SELECT invocation_id, type, ROW_NUMBER() OVER (ORDER BY sequence) AS ordinal
    FROM _invocations
),
ranked_events AS (
    SELECT event_id,
           CASE kind
               WHEN 'opening_invocation' THEN 'opening'
               WHEN 'call_invocation' THEN 'call'
               WHEN 'closing_invocation' THEN 'closing'
           END AS type,
           ROW_NUMBER() OVER (ORDER BY applied_at_ms, event_id) AS ordinal
    FROM _events
    WHERE status = 'applied'
      AND kind IN ('opening_invocation', 'call_invocation', 'closing_invocation')
)
UPDATE _invocations
SET origin_event_id = (
    SELECT ranked_events.event_id
    FROM ranked_invocations
    JOIN ranked_events
      ON ranked_events.ordinal = ranked_invocations.ordinal
     AND ranked_events.type = ranked_invocations.type
    WHERE ranked_invocations.invocation_id = _invocations.invocation_id
);
CREATE TEMP TABLE _invocation_origin_guard (
    violations INTEGER NOT NULL CHECK (violations = 0)
);
INSERT INTO _invocation_origin_guard (violations)
SELECT (SELECT COUNT(*) FROM _invocations WHERE origin_event_id IS NULL)
     + ABS(
           (SELECT COUNT(*) FROM _invocations)
         - (SELECT COUNT(*) FROM _events
            WHERE status = 'applied'
              AND kind IN ('opening_invocation', 'call_invocation', 'closing_invocation'))
       );
DROP TABLE _invocation_origin_guard;
CREATE UNIQUE INDEX _invocations_origin ON _invocations (origin_event_id);
INSERT INTO _visibility_registry ("table", "column", visibility_class)
VALUES
    ('_run', 'final_score_cents', 'post_run_only'),
    ('_run', 'track', 'post_run_only'),
    ('_run', 'attempt_definition', 'post_run_only'),
    ('_invocations', 'origin_event_id', 'post_run_only')
ON CONFLICT ("table", "column") DO UPDATE SET visibility_class = excluded.visibility_class;
