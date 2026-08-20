
ALTER TABLE _technicians ADD COLUMN home_address_catalog_id INTEGER
    REFERENCES _address_catalog(id) ON DELETE RESTRICT;

ALTER TABLE _appointments ADD COLUMN lateness_minutes INTEGER;
