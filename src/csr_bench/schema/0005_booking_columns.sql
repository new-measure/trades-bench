-- Migration 0005: booking support columns added to existing tables.
-- home_address_catalog_id links a technician to their private catalog
-- address and is private_engine, matching _locations.address_catalog_id.
-- lateness_minutes joins the rest of _appointments as public_sql; it stays
-- null until work starts and is filled by a later phase.
--
-- Conventions: <entity>_id is an FK with ON DELETE RESTRICT; SQLite resolves
-- forward references lazily at DML time (see 0001_baseline.sql).

ALTER TABLE _technicians ADD COLUMN home_address_catalog_id INTEGER
    REFERENCES _address_catalog(id) ON DELETE RESTRICT;

ALTER TABLE _appointments ADD COLUMN lateness_minutes INTEGER;
