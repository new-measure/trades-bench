
CREATE TABLE _weather_process_state (
    singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
    seasonal_amplitude_f    INTEGER NOT NULL,
    seasonal_phase_days     INTEGER NOT NULL,
    final_x_milli           INTEGER NOT NULL,
    precompute_start_date   TEXT NOT NULL,
    precompute_end_date     TEXT NOT NULL,
    created_at              INTEGER NOT NULL
);

CREATE TABLE _zone_weather_daily (
    zone_id                  INTEGER NOT NULL REFERENCES _zones(id) ON DELETE RESTRICT,
    local_date               TEXT NOT NULL,
    high_f_tenths            INTEGER NOT NULL,
    low_f_tenths             INTEGER NOT NULL,
    precipitation_mm_tenths  INTEGER NOT NULL,
    wind_mph_tenths          INTEGER NOT NULL,
    storm_flag               INTEGER NOT NULL CHECK (storm_flag IN (0,1)),
    storm_severity_milli     INTEGER,
    condition_code           TEXT NOT NULL CHECK (condition_code IN
                               ('clear','cool','hot','windy','extreme_heat','rain','storm')),
    created_at               INTEGER NOT NULL,
    PRIMARY KEY (zone_id, local_date),
    CHECK (storm_flag = 0 OR storm_severity_milli IS NOT NULL)
);

CREATE TRIGGER trg_zone_weather_daily_no_update
BEFORE UPDATE ON _zone_weather_daily
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_zone_weather_daily_no_delete
BEFORE DELETE ON _zone_weather_daily
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TABLE _weather_forecast_cache (
    zone_id          INTEGER NOT NULL REFERENCES _zones(id) ON DELETE RESTRICT,
    query_date       TEXT NOT NULL,
    target_date      TEXT NOT NULL,
    high_hat_f       INTEGER NOT NULL,
    low_hat_f        INTEGER NOT NULL,
    storm_hat        INTEGER NOT NULL CHECK (storm_hat IN (0,1)),
    condition_code   TEXT NOT NULL CHECK (condition_code IN
                       ('clear','cool','hot','windy','extreme_heat','rain','storm')),
    first_issued_at  INTEGER NOT NULL,
    PRIMARY KEY (zone_id, query_date, target_date)
);

CREATE TRIGGER trg_weather_forecast_cache_no_update
BEFORE UPDATE ON _weather_forecast_cache
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_weather_forecast_cache_no_delete
BEFORE DELETE ON _weather_forecast_cache
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TABLE _pending_calls (
    id                      INTEGER PRIMARY KEY,
    service_date            TEXT NOT NULL,
    zone_id                 INTEGER REFERENCES _zones(id) ON DELETE RESTRICT,
    arrival_at              INTEGER NOT NULL,
    call_kind               TEXT NOT NULL CHECK (call_kind IN ('new_job','cancellation')),
    channel                 TEXT NOT NULL CHECK (channel IN
                              ('new_caller','existing_customer','cancellation','callback')),
    segment                 TEXT NOT NULL CHECK (segment IN
                              ('residential','commercial','apartment')),
    requested_category_id   INTEGER NOT NULL REFERENCES _service_catalog(id) ON DELETE RESTRICT,
    urgent                  INTEGER NOT NULL CHECK (urgent IN (0,1)),
    customer_id             INTEGER REFERENCES _customers(id) ON DELETE RESTRICT,
    location_id             INTEGER REFERENCES _locations(id) ON DELETE RESTRICT,
    address_catalog_id      INTEGER REFERENCES _address_catalog(id) ON DELETE RESTRICT,
    caller_name             TEXT NOT NULL,
    caller_phone            TEXT NOT NULL,
    prospect_id             INTEGER REFERENCES _prospect_state(id) ON DELETE RESTRICT,
    related_job_id          INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    slot_index              INTEGER,
    event_id                INTEGER REFERENCES _events(event_id) ON DELETE RESTRICT,
    created_at              INTEGER NOT NULL
);
CREATE INDEX _pending_calls_order ON _pending_calls (service_date, arrival_at);

CREATE TABLE _call_hidden (
    call_id                 INTEGER PRIMARY KEY REFERENCES _calls(id) ON DELETE RESTRICT,
    true_segment            TEXT NOT NULL CHECK (true_segment IN
                              ('residential','commercial','apartment')),
    requested_category_id   INTEGER NOT NULL REFERENCES _service_catalog(id) ON DELETE RESTRICT,
    prospect_id             INTEGER REFERENCES _prospect_state(id) ON DELETE RESTRICT,
    acceptance_result       INTEGER,
    created_at              INTEGER NOT NULL
);

CREATE TABLE _customer_repeat_state (
    customer_id            INTEGER PRIMARY KEY REFERENCES _customers(id) ON DELETE RESTRICT,
    propensity_micros       INTEGER NOT NULL,
    last_outcome             TEXT,
    competitor_captured_at   INTEGER,
    updated_at               INTEGER NOT NULL
);

CREATE TABLE _prospect_state (
    id                      INTEGER PRIMARY KEY,
    identity_key            TEXT NOT NULL UNIQUE,
    caller_name             TEXT NOT NULL,
    caller_phone            TEXT NOT NULL,
    segment                 TEXT NOT NULL CHECK (segment IN
                              ('residential','commercial','apartment')),
    home_zone_id            INTEGER REFERENCES _zones(id) ON DELETE RESTRICT,
    propensity_micros       INTEGER NOT NULL,
    last_outcome            TEXT,
    competitor_captured_at  INTEGER,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);

CREATE TABLE _word_of_mouth_state (
    segment    TEXT PRIMARY KEY CHECK (segment IN ('residential','commercial','apartment')),
    wom_micros INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE _competitor_state (
    singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
    share_micros  INTEGER NOT NULL,
    capture_count INTEGER NOT NULL,
    drift_micros  INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE _acceptance_parameters (
    urgency_class TEXT PRIMARY KEY CHECK (urgency_class IN ('standard','urgent')),
    base_micros   INTEGER NOT NULL,
    decay_micros  INTEGER NOT NULL,
    floor_micros  INTEGER NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE _cancellation_schedule (
    appointment_id INTEGER PRIMARY KEY REFERENCES _appointments(id) ON DELETE RESTRICT,
    cancel_exists  INTEGER NOT NULL CHECK (cancel_exists IN (0,1)),
    arrival_at     INTEGER,
    event_id       INTEGER REFERENCES _events(event_id) ON DELETE RESTRICT,
    created_at     INTEGER NOT NULL
);
