
ALTER TABLE _prospect_state ADD COLUMN registered_customer_id INTEGER REFERENCES _customers(id);
INSERT INTO _visibility_registry ("table", "column", visibility_class)
VALUES ('_prospect_state', 'registered_customer_id', 'private_engine')
ON CONFLICT ("table", "column") DO UPDATE SET visibility_class = excluded.visibility_class;
UPDATE _prospect_state
SET registered_customer_id = (
    SELECT c.customer_id FROM _calls c
    JOIN _call_hidden ch ON ch.call_id = c.id
    WHERE ch.prospect_id = _prospect_state.id AND c.customer_id IS NOT NULL
    ORDER BY c.id LIMIT 1
)
WHERE registered_customer_id IS NULL;
