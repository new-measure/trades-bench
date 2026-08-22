CREATE INDEX idx_locations_address_catalog ON _locations(address_catalog_id);
CREATE INDEX idx_address_pool_zone_residential ON _address_catalog(zone_id, id) WHERE status = 'usable' AND (',' || segment_eligibility || ',') LIKE '%,residential,%';
CREATE INDEX idx_address_pool_eff_residential ON _address_catalog(effective_zone_id, id) WHERE status = 'usable' AND (',' || segment_eligibility || ',') LIKE '%,residential,%';
CREATE INDEX idx_address_pool_zone_commercial ON _address_catalog(zone_id, id) WHERE status = 'usable' AND (',' || segment_eligibility || ',') LIKE '%,commercial,%';
CREATE INDEX idx_address_pool_eff_commercial ON _address_catalog(effective_zone_id, id) WHERE status = 'usable' AND (',' || segment_eligibility || ',') LIKE '%,commercial,%';
CREATE INDEX idx_address_pool_zone_apartment ON _address_catalog(zone_id, id) WHERE status = 'usable' AND (',' || segment_eligibility || ',') LIKE '%,apartment,%';
CREATE INDEX idx_address_pool_eff_apartment ON _address_catalog(effective_zone_id, id) WHERE status = 'usable' AND (',' || segment_eligibility || ',') LIKE '%,apartment,%';
