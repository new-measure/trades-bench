-- Migration 0006: call arrival linkage. arrival_event_id ties each
-- _call_hidden row to the call_invocation event that created it, so the
-- engine can recover the permanent call for an invocation after the
-- _pending_calls row is deleted. private_engine like the rest of the table.

ALTER TABLE _call_hidden ADD COLUMN arrival_event_id INTEGER
    REFERENCES _events(event_id) ON DELETE RESTRICT;
