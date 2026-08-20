
ALTER TABLE _call_hidden ADD COLUMN arrival_event_id INTEGER
    REFERENCES _events(event_id) ON DELETE RESTRICT;
