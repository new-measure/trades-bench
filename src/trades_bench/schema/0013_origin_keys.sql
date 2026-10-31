ALTER TABLE _pending_calls ADD COLUMN origin_key TEXT;
ALTER TABLE _call_hidden ADD COLUMN origin_key TEXT;
ALTER TABLE _customer_repeat_state ADD COLUMN origin_key TEXT;
ALTER TABLE _events ADD COLUMN order_key TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX _pending_calls_origin_key ON _pending_calls (origin_key);
CREATE UNIQUE INDEX _call_hidden_origin_key ON _call_hidden (origin_key);
CREATE UNIQUE INDEX _customer_repeat_state_origin_key ON _customer_repeat_state (origin_key);
DROP INDEX _events_order;
CREATE INDEX _events_order ON _events (scheduled_at_ms, priority, order_key, event_id);
INSERT INTO _visibility_registry ("table", "column", visibility_class)
VALUES ('_pending_calls', 'origin_key', 'private_engine'),
       ('_call_hidden', 'origin_key', 'private_engine'),
       ('_customer_repeat_state', 'origin_key', 'private_engine'),
       ('_events', 'order_key', 'private_engine')
ON CONFLICT ("table", "column") DO UPDATE SET visibility_class = excluded.visibility_class;
