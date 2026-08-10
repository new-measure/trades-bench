-- Migration 0001: the complete spec-02 v1 schema as one fresh baseline
-- (CSR-002 decision 1: no evolution from CSR-001 — a run is a sealed artifact).
--
-- Transcribed from specs/02-data-and-initial-state.md §1.3-1.4, §2, §4, §5, §6
-- plus specs/01-run-contracts.md (run record), specs/03 §2.1/§3.1 (zones,
-- address catalog), specs/04 §1.5 (hidden technician state), specs/05 §4.2 and
-- specs/06 §9.1 (service catalog, ledger enum).
--
-- §1.3 conventions: <name>_at / <name>_ms = UTC epoch milliseconds; <name>_date /
-- <name>_on = TEXT 'YYYY-MM-DD' Phoenix-local; money = integer cents with a
-- _cents suffix; booleans = INTEGER 0/1 with CHECK. `origin` (CHECK
-- IN ('warm_start','run'), DEFAULT 'run') rides only the 15 root-entity tables
-- (§1.3); child and append-only rows do not carry it.
-- §6.2: every <entity>_id is an FK with ON DELETE RESTRICT; the only CASCADE is
-- _config.run_id -> _run (decision 5). Nullability follows §4's column lists
-- (with §6.2's FKs); see docs/tickets/CSR-002.md implementation notes for the
-- §6.2 stale-list reconciliation.
-- §6.4: same-table CHECKs only. The two cross-table block rules become BEFORE
-- INSERT triggers on _appointment_blocks (decision 14 — SQLite forbids
-- subqueries in CHECK). The six append-only tables of §1.4 other than _events
-- (decision 15's carve-out) get permanent no-update / no-delete triggers.
-- Seed rows carry created_at = 0, the pre-world sentinel.

-- ============================================================================
-- A. Core run-contract tables (spec 02 §5; decisions 2-5)
-- ============================================================================

CREATE TABLE _run (
    run_id              TEXT PRIMARY KEY,
    spec_version        TEXT NOT NULL,
    config_version      TEXT NOT NULL,
    config_hash         TEXT NOT NULL,
    master_seed         INTEGER NOT NULL,
    start_at_ms         INTEGER NOT NULL,
    first_service_date  TEXT NOT NULL,
    last_service_date   TEXT NOT NULL,
    timezone            TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                          ('initializing','active','completed','bankrupt','failed')),
    model_id            TEXT,
    harness_name        TEXT,
    harness_version     TEXT,
    context_policy      TEXT,
    system_prompt_hash  TEXT
);

-- Decision 5: the resolved config leaves _run as a durable sibling row.
CREATE TABLE _config (
    run_id          TEXT PRIMARY KEY REFERENCES _run(run_id) ON DELETE CASCADE,
    config_version  TEXT NOT NULL,
    canonical_json  TEXT NOT NULL
);

-- Decision 4: no run_id columns outside _run. Event envelope per spec 01
-- §Event envelope and ordering.
CREATE TABLE _events (
    event_id        INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    scheduled_at_ms INTEGER NOT NULL,
    priority        INTEGER NOT NULL,
    entity_type     TEXT,
    entity_id       INTEGER,
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload         TEXT,
    status          TEXT NOT NULL CHECK (status IN
                      ('pending','processing','applied','cancelled')),
    created_at_ms   INTEGER NOT NULL,
    applied_at_ms   INTEGER
);
CREATE INDEX _events_order ON _events (scheduled_at_ms, priority, event_id);
CREATE INDEX _events_status ON _events (status);

CREATE TABLE _idempotency (
    namespace       TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    response        BLOB NOT NULL,
    created_at_ms   INTEGER NOT NULL,
    PRIMARY KEY (namespace, request_id)
);

CREATE TABLE _invocations (
    invocation_id   INTEGER PRIMARY KEY,
    type            TEXT NOT NULL,
    ts_ms           INTEGER NOT NULL,
    sequence        INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active','completed')),
    prompt_version  TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX _invocations_order ON _invocations (sequence);

CREATE TABLE _rng_state (
    stream_name TEXT PRIMARY KEY,
    seed        TEXT NOT NULL,
    counter     INTEGER NOT NULL
);

CREATE TABLE _random_audit (
    seq             INTEGER PRIMARY KEY,
    stream_name     TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       INTEGER NOT NULL,
    scope           TEXT NOT NULL,
    event_kind      TEXT NOT NULL,
    draw_index      INTEGER NOT NULL,
    outcome_id      TEXT,
    simulated_ts_ms INTEGER NOT NULL
);
CREATE INDEX _random_audit_key ON _random_audit
    (stream_name, entity_type, entity_id, scope, event_kind, draw_index);

-- Spec 02 §1.5: simulated time lives here and only here (decision 3); _run keeps
-- only its immutable boundary fields.
CREATE TABLE _clock (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_time_ms INTEGER NOT NULL,
    service_date    TEXT NOT NULL
);

-- Durable copy of the Python visibility registry (decision 5).
-- `table` and `column` are SQLite reserved words; quoted identifiers keep the
-- spec 02 §5 column names verbatim.
CREATE TABLE _visibility_registry (
    "table"          TEXT NOT NULL,
    "column"         TEXT NOT NULL,
    visibility_class TEXT NOT NULL,
    PRIMARY KEY ("table", "column")
);

-- Spec 02 §9: applied-migration ledger. The runner bootstraps this table itself
-- before applying files; creating it here is harmless and simpler.
CREATE TABLE _schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  INTEGER NOT NULL
);

-- ============================================================================
-- B. Reference and catalog data (spec 02 §2) with deterministic seeds
-- ============================================================================

-- spec 02 §2.1: exactly three business units (Q96).
CREATE TABLE _business_units (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at  INTEGER NOT NULL
);

INSERT INTO _business_units (id, code, name, active, created_at) VALUES
    (1, 'hvac',       'HVAC',       1, 0),
    (2, 'plumbing',   'Plumbing',   1, 0),
    (3, 'electrical', 'Electrical', 1, 0);

-- spec 02 §2.2: the closed qualification-code set (spec 04 §1.2).
CREATE TABLE _skills (
    id                INTEGER PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    business_unit_id  INTEGER NOT NULL REFERENCES _business_units(id) ON DELETE RESTRICT,
    active            INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at        INTEGER NOT NULL
);

INSERT INTO _skills (id, code, name, business_unit_id, active, created_at) VALUES
    (1, 'hvac_service', 'HVAC Service', 1, 1, 0),
    (2, 'hvac_install', 'HVAC Install', 1, 1, 0),
    (3, 'plumbing',     'Plumbing',     2, 1, 0),
    (4, 'electrical',   'Electrical',   3, 1, 0);

-- spec 02 §2.3: the four supported types plus the misbooking sink.
CREATE TABLE _job_types (
    id                  INTEGER PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    business_unit_id    INTEGER REFERENCES _business_units(id) ON DELETE RESTRICT,
    scheduled_blocks    INTEGER NOT NULL,
    required_skill_code TEXT REFERENCES _skills(code) ON DELETE RESTRICT,
    active              INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at          INTEGER NOT NULL
);

INSERT INTO _job_types (id, code, name, business_unit_id, scheduled_blocks,
                        required_skill_code, active, created_at) VALUES
    (1, 'hvac_service',     'HVAC Service',     1, 1, 'hvac_service', 1, 0),
    (2, 'hvac_installation','HVAC Installation',1, 2, 'hvac_install', 1, 0),
    (3, 'plumbing',         'Plumbing',         2, 1, 'plumbing',     1, 0),
    (4, 'electrical',       'Electrical',       3, 1, 'electrical',   1, 0),
    (5, 'other',            'Other',            NULL, 1, NULL,        1, 0);

-- spec 02 §2.4 / spec 03 §2.1: ten zones in canonical order. The city/postal
-- lists are provisional placeholders recorded for CSR-003 validation (decision
-- 8: polygons and geometry belong to zone_definitions.geojson).
CREATE TABLE _zones (
    id                 INTEGER PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    cities_json        TEXT NOT NULL,
    postal_codes_json  TEXT NOT NULL,
    active             INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at         INTEGER NOT NULL
);

INSERT INTO _zones (id, code, name, cities_json, postal_codes_json, active, created_at) VALUES
    (1,  'central_phoenix',     'Central Phoenix',     '["Central Phoenix","Phoenix"]',
        '["85001","85003","85004","85006","85007","85013","85016"]', 1, 0),
    (2,  'north_phoenix',       'North Phoenix',       '["North Phoenix","Phoenix"]',
        '["85020","85022","85024","85027","85028","85029","85032","85050","85051","85053","85054","85085","85086","85087"]', 1, 0),
    (3,  'scottsdale',          'Scottsdale',          '["Scottsdale"]',
        '["85250","85251","85254","85255","85257","85258","85259","85260","85262"]', 1, 0),
    (4,  'tempe',               'Tempe',               '["Tempe"]',
        '["85281","85282","85283","85284","85285","85287","85288"]', 1, 0),
    (5,  'mesa',                'Mesa',                '["Mesa"]',
        '["85201","85202","85203","85204","85205","85206","85207","85208","85209","85210","85211","85212","85213","85215","85275","85277"]', 1, 0),
    (6,  'chandler',            'Chandler',            '["Chandler"]',
        '["85224","85225","85226","85227","85244","85246","85248","85249","85286"]', 1, 0),
    (7,  'gilbert',             'Gilbert',             '["Gilbert"]',
        '["85233","85234","85295","85296","85297","85298","85299"]', 1, 0),
    (8,  'glendale',            'Glendale',            '["Glendale"]',
        '["85301","85302","85303","85304","85305","85306","85307","85308","85310","85311","85312","85318"]', 1, 0),
    (9,  'peoria',              'Peoria',              '["Peoria"]',
        '["85345","85381","85382","85383","85385","85387"]', 1, 0),
    (10, 'west_valley_goodyear','West Valley/Goodyear','["Goodyear","Avondale","Litchfield Park"]',
        '["85338","85340","85395","85396"]', 1, 0);

-- spec 02 §2.4: supports the private nearest-zone effective_zone_id assignment.
CREATE TABLE _zone_geometry (
    zone_id         INTEGER PRIMARY KEY REFERENCES _zones(id) ON DELETE RESTRICT,
    centroid_lat    REAL NOT NULL,
    centroid_lon    REAL NOT NULL,
    boundary_ref    TEXT
);

-- spec 02 §2.5: private requested-service catalog. The supported mapping must
-- never be resolvable by a public join (SCH-07).
CREATE TABLE _service_catalog (
    id                    INTEGER PRIMARY KEY,
    catalog_version       TEXT NOT NULL,
    name                  TEXT NOT NULL,
    supported_job_type_id INTEGER REFERENCES _job_types(id) ON DELETE RESTRICT,
    demand_weight_key     TEXT NOT NULL,
    active                INTEGER NOT NULL CHECK (active IN (0,1))
);

-- spec 05 §4.2: the four normative supported categories plus the six named
-- unsupported defaults, under a fixed catalog version (decision 8).
INSERT INTO _service_catalog (id, catalog_version, name, supported_job_type_id,
                              demand_weight_key, active) VALUES
    (1,  '1.0.0', 'HVAC installation',  2,    'hvac_installation', 1),
    (2,  '1.0.0', 'HVAC service',       1,    'hvac_service',      1),
    (3,  '1.0.0', 'Plumbing',           3,    'plumbing',          1),
    (4,  '1.0.0', 'Electrical',         4,    'electrical',        1),
    (5,  '1.0.0', 'Appliance repair',   NULL, 'appliance_repair',  1),
    (6,  '1.0.0', 'Garage-door repair', NULL, 'garage_door_repair',1),
    (7,  '1.0.0', 'Roofing',            NULL, 'roofing',           1),
    (8,  '1.0.0', 'Pool service',       NULL, 'pool_service',      1),
    (9,  '1.0.0', 'Pest control',       NULL, 'pest_control',      1),
    (10, '1.0.0', 'Landscaping',        NULL, 'landscaping',       1);

-- spec 02 §2.6 / spec 03 §3.1: authoritative address-catalog columns. No unit
-- column (units live on locations). segment_eligibility is a JSON array of
-- {residential, apartment, commercial}. Empty until CSR-003.
CREATE TABLE _address_catalog (
    id                   INTEGER PRIMARY KEY,
    dataset_version      TEXT NOT NULL,
    house_number         TEXT NOT NULL,
    street               TEXT NOT NULL,
    city                 TEXT NOT NULL,
    state                TEXT NOT NULL,
    postal_code          TEXT NOT NULL,
    formatted_address    TEXT NOT NULL,
    canonical_key        TEXT NOT NULL UNIQUE,
    latitude             REAL NOT NULL,
    longitude            REAL NOT NULL,
    zone_id              INTEGER REFERENCES _zones(id) ON DELETE RESTRICT,
    effective_zone_id    INTEGER NOT NULL,
    anchor_id            TEXT NOT NULL,
    access_distance_km   REAL NOT NULL,
    segment_eligibility  TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('usable','unusable')),
    unusable_reason      TEXT,
    created_at           INTEGER NOT NULL
);

-- spec 02 §2.6: frozen-dataset identity; empty until CSR-003.
CREATE TABLE _dataset_versions (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    version        TEXT NOT NULL,
    snapshot_date  TEXT,
    checksum       TEXT NOT NULL,
    attribution    TEXT,
    created_at     INTEGER NOT NULL
);

-- ============================================================================
-- C. Domain tables — transcribed verbatim from spec 02 §4.1-§4.8
-- ============================================================================

-- §4.1 CRM ----------------------------------------------------------------
-- _customers carries origin (root entity) and references _calls (forward
-- reference; SQLite resolves FK parents lazily at DML time).
CREATE TABLE _customers (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    segment              TEXT NOT NULL CHECK (segment IN ('commercial','apartment','residential')),
    status               TEXT NOT NULL CHECK (status IN ('active','inactive')),
    created_at           INTEGER NOT NULL,
    created_from_call_id INTEGER REFERENCES _calls(id) ON DELETE RESTRICT,
    modified_at          INTEGER NOT NULL,
    origin               TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

CREATE TABLE _customer_contacts (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    contact_type TEXT NOT NULL CHECK (contact_type IN ('phone','email')),
    value        TEXT NOT NULL,
    do_not_text  INTEGER NOT NULL CHECK (do_not_text IN (0,1)),
    memo         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

CREATE TABLE _locations (
    id                  INTEGER PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    name                TEXT NOT NULL,
    street              TEXT NOT NULL,
    unit                TEXT NOT NULL,
    city                TEXT NOT NULL,
    state               TEXT NOT NULL,
    postal_code         TEXT NOT NULL,
    latitude            REAL NOT NULL,
    longitude           REAL NOT NULL,
    zone_id             INTEGER REFERENCES _zones(id) ON DELETE RESTRICT,
    active              INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at          INTEGER NOT NULL,
    modified_at         INTEGER NOT NULL,
    effective_zone_id   INTEGER NOT NULL,
    address_catalog_id  INTEGER REFERENCES _address_catalog(id) ON DELETE RESTRICT,
    origin              TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

CREATE TABLE _location_contacts (
    id           INTEGER PRIMARY KEY,
    location_id  INTEGER NOT NULL REFERENCES _locations(id) ON DELETE RESTRICT,
    contact_type TEXT NOT NULL CHECK (contact_type IN ('phone','email')),
    value        TEXT NOT NULL,
    do_not_text  INTEGER NOT NULL CHECK (do_not_text IN (0,1)),
    memo         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

-- §4.2 Calls ---------------------------------------------------------------
CREATE TABLE _calls (
    id                      INTEGER PRIMARY KEY,
    arrived_at              INTEGER NOT NULL,
    service_date            TEXT NOT NULL,
    call_kind               TEXT NOT NULL CHECK (call_kind IN ('new_job','cancellation')),
    status                  TEXT NOT NULL CHECK (status IN
                              ('active','booked','declined','rejected','acknowledged','unanswered')),
    caller_name             TEXT NOT NULL,
    caller_phone            TEXT NOT NULL,
    segment                 TEXT,
    customer_id             INTEGER REFERENCES _customers(id) ON DELETE RESTRICT,
    location_id             INTEGER REFERENCES _locations(id) ON DELETE RESTRICT,
    service_street          TEXT,
    service_unit            TEXT,
    service_city            TEXT,
    service_state           TEXT,
    service_postal_code     TEXT,
    requested_service_name  TEXT NOT NULL,
    urgent                  INTEGER NOT NULL CHECK (urgent IN (0,1)),
    related_job_id          INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    target_appointment_id   INTEGER REFERENCES _appointments(id) ON DELETE RESTRICT,
    target_job_id           INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    created_job_id          INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    terminal_at             INTEGER,
    terminal_reason         TEXT CHECK (terminal_reason IN
                              ('accepted','customer_declined','agent_rejected',
                               'cancellation_acknowledged','unanswered')),
    created_at              INTEGER NOT NULL,
    origin                  TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    address_catalog_id      INTEGER REFERENCES _address_catalog(id) ON DELETE RESTRICT
);

-- §1.4 append-only; one row per completed attempt (spec 02 §4.2 note).
CREATE TABLE _call_attempts (
    id             INTEGER PRIMARY KEY,
    call_id        INTEGER NOT NULL REFERENCES _calls(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL,
    prompted_at    INTEGER NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('incomplete','resolved')),
    created_at     INTEGER NOT NULL
);

-- §4.3 Jobs -----------------------------------------------------------------
CREATE TABLE _jobs (
    id                      INTEGER PRIMARY KEY,
    job_number              TEXT NOT NULL UNIQUE,
    call_id                 INTEGER NOT NULL REFERENCES _calls(id) ON DELETE RESTRICT,
    customer_id             INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    location_id             INTEGER NOT NULL REFERENCES _locations(id) ON DELETE RESTRICT,
    job_type_id             INTEGER NOT NULL REFERENCES _job_types(id) ON DELETE RESTRICT,
    business_unit_id        INTEGER REFERENCES _business_units(id) ON DELETE RESTRICT,
    requested_service_name  TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN
                              ('scheduled','in_progress','completed','failed','no_show','cancelled')),
    outcome                 TEXT CHECK (outcome IN
                              ('completed_paid','completed_warranty','failed_unsupported',
                               'failed_unqualified','no_show_company','cancelled_by_customer')),
    priority                TEXT NOT NULL CHECK (priority IN ('urgent','normal')),
    no_charge               INTEGER NOT NULL CHECK (no_charge IN (0,1)),
    recall_for_job_id       INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    summary                 TEXT NOT NULL,
    created_at              INTEGER NOT NULL,
    completed_at            INTEGER,
    modified_at             INTEGER NOT NULL,
    origin                  TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

-- §4.4 Appointments, blocks, assignments, events ---------------------------
CREATE TABLE _appointments (
    id                   INTEGER PRIMARY KEY,
    appointment_number   TEXT NOT NULL UNIQUE,
    job_id               INTEGER NOT NULL REFERENCES _jobs(id) ON DELETE RESTRICT,
    status               TEXT NOT NULL CHECK (status IN
                            ('scheduled','dispatched','in_progress','completed',
                             'failed','no_show','cancelled')),
    scheduled_start      INTEGER NOT NULL,
    scheduled_end        INTEGER NOT NULL,
    arrival_window_start INTEGER NOT NULL,
    arrival_window_end   INTEGER NOT NULL,
    actual_start         INTEGER,
    actual_end           INTEGER,
    cancelled_at         INTEGER,
    created_at           INTEGER NOT NULL,
    modified_at          INTEGER NOT NULL,
    origin               TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

-- §4.4 / §6.4 + decision 14: block_start/block_end are epoch-ms timestamps
-- (§1.3), so grid membership is a time-of-day CHECK computed in pure SQL over
-- the fixed −07:00 Phoenix offset (no subqueries; strftime IS allowed in
-- CHECKs): block_end = block_start + 2h and block_start's Phoenix time-of-day
-- is one of the six grid starts 07:00/09:00/11:00/13:00/15:00/17:00. The two
-- cross-table rules are the BEFORE INSERT triggers of section D.
CREATE TABLE _appointment_blocks (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES _appointments(id) ON DELETE RESTRICT,
    ordinal        INTEGER NOT NULL,
    block_code     TEXT NOT NULL CHECK (block_code IN
                      ('B07_09','B09_11','B11_13','B13_15','B15_17','B17_19')),
    block_start    INTEGER NOT NULL,
    block_end      INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    UNIQUE (appointment_id, ordinal),
    CHECK (
      block_end = block_start + 7200000
      AND (CAST(strftime('%H', block_start / 1000, 'unixepoch', '-7 hours') AS INTEGER) * 60
           + CAST(strftime('%M', block_start / 1000, 'unixepoch', '-7 hours') AS INTEGER))
          IN (420, 540, 660, 780, 900, 1020)
    )
);

CREATE TABLE _appointment_assignments (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES _appointments(id) ON DELETE RESTRICT,
    job_id         INTEGER NOT NULL REFERENCES _jobs(id) ON DELETE RESTRICT,
    technician_id  INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    status         TEXT NOT NULL CHECK (status IN ('active','reassigned','cancelled')),
    assigned_at    INTEGER NOT NULL,
    unassigned_at  INTEGER,
    assigned_by    TEXT NOT NULL CHECK (assigned_by IN ('agent','warm_start','controller')),
    created_at     INTEGER NOT NULL
);
-- spec 02 §6.3: at most one active assignment per appointment.
CREATE UNIQUE INDEX _appointment_assignments_active
    ON _appointment_assignments (appointment_id) WHERE status = 'active';

-- §1.4 append-only; the eight-member event_type set is the enforced copy of
-- spec 03 §8.5 + spec 06 §1.1 (spec 02 §4.4 note).
CREATE TABLE _appointment_events (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES _appointments(id) ON DELETE RESTRICT,
    technician_id  INTEGER REFERENCES _technicians(id) ON DELETE RESTRICT,
    event_type     TEXT NOT NULL CHECK (event_type IN
                      ('dispatched','arrived','work_started','work_completed',
                       'work_failed','no_show','cancelled','reassigned')),
    occurred_at    INTEGER NOT NULL,
    service_date   TEXT NOT NULL,
    detail         TEXT NOT NULL,
    created_at     INTEGER NOT NULL
);

-- §4.4: the structural overlap guard. block_start is an epoch-ms timestamp
-- equal to an appointment block's start (WS-01 rule 7 compares it to
-- '2026-10-01'); no bounds CHECK — only the PK. Rows are deleted when capacity
-- is released — the one deletion exception to §1.4 (spec 02 §4.4 note).
CREATE TABLE _technician_block_holds (
    technician_id           INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    block_start             INTEGER NOT NULL,
    hold_kind               TEXT NOT NULL CHECK (hold_kind IN ('job','training','absence')),
    appointment_id          INTEGER REFERENCES _appointments(id) ON DELETE RESTRICT,
    non_job_appointment_id  INTEGER REFERENCES _non_job_appointments(id) ON DELETE RESTRICT,
    created_at              INTEGER NOT NULL,
    PRIMARY KEY (technician_id, block_start),
    CHECK ((appointment_id IS NULL) != (non_job_appointment_id IS NULL))
);

-- §4.5 Workforce ------------------------------------------------------------
CREATE TABLE _technicians (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    street                TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    city                  TEXT NOT NULL,
    state                 TEXT NOT NULL,
    postal_code           TEXT NOT NULL,
    home_latitude         REAL NOT NULL,
    home_longitude        REAL NOT NULL,
    home_zone_id          INTEGER REFERENCES _zones(id) ON DELETE RESTRICT,
    joined_on             TEXT NOT NULL,
    hourly_wage_cents     INTEGER NOT NULL,
    employment_status     TEXT NOT NULL CHECK (employment_status IN
                            ('hired_pending_start','active','pending_termination',
                             'resigning_notice','terminated','resigned')),
    availability_status   TEXT NOT NULL CHECK (availability_status IN ('available','sick')),
    available_from        TEXT,
    final_working_date    TEXT,
    created_at            INTEGER NOT NULL,
    modified_at           INTEGER NOT NULL,
    origin                TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    CHECK (employment_status NOT IN ('terminated','resigned')
           OR final_working_date IS NOT NULL)
);

CREATE TABLE _technician_skills (
    id             INTEGER PRIMARY KEY,
    technician_id  INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    skill_code     TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    active         INTEGER NOT NULL CHECK (active IN (0,1)),
    granted_on     TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    origin         TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    UNIQUE (technician_id, skill_code)
);

CREATE TABLE _technician_shifts (
    id                   INTEGER PRIMARY KEY,
    technician_id        INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    shift_type           TEXT NOT NULL CHECK (shift_type IN ('office','after_hours','weekend')),
    start_at             INTEGER NOT NULL,
    end_at               INTEGER NOT NULL,
    service_date         TEXT NOT NULL,
    active               INTEGER NOT NULL CHECK (active IN (0,1)),
    on_call_rotation_id  INTEGER REFERENCES _on_call_rotations(id) ON DELETE RESTRICT,
    created_at           INTEGER NOT NULL,
    origin               TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

CREATE TABLE _technician_locations (
    technician_id               INTEGER PRIMARY KEY REFERENCES _technicians(id) ON DELETE RESTRICT,
    latitude                    REAL NOT NULL,
    longitude                   REAL NOT NULL,
    coordinates_updated_at      INTEGER NOT NULL,
    location_state              TEXT NOT NULL CHECK (location_state IN ('home','traveling','on_site')),
    trip_origin_latitude        REAL,
    trip_origin_longitude       REAL,
    trip_destination_latitude   REAL,
    trip_destination_longitude  REAL,
    trip_departed_at            INTEGER,
    trip_travel_minutes         INTEGER
);

-- §4.5 / spec 03 §9.1: appointment-independent legs, the return-home leg.
CREATE TABLE _technician_movements (
    id                      INTEGER PRIMARY KEY,
    technician_id           INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    service_date            TEXT NOT NULL,
    movement_kind           TEXT NOT NULL CHECK (movement_kind IN ('return_home')),
    origin_latitude         REAL NOT NULL,
    origin_longitude        REAL NOT NULL,
    destination_latitude    REAL NOT NULL,
    destination_longitude   REAL NOT NULL,
    departed_at             INTEGER NOT NULL,
    arrives_at              INTEGER NOT NULL,
    created_at              INTEGER NOT NULL
);

CREATE TABLE _technician_absences (
    id                    INTEGER PRIMARY KEY,
    technician_id         INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    absence_kind          TEXT NOT NULL CHECK (absence_kind IN ('sick')),
    start_date            TEXT NOT NULL,
    expected_return_date  TEXT NOT NULL,
    actual_return_date    TEXT,
    status                TEXT NOT NULL CHECK (status IN ('active','ended')),
    created_at            INTEGER NOT NULL
);

CREATE TABLE _non_job_appointments (
    id                       INTEGER PRIMARY KEY,
    technician_id            INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    kind                     TEXT NOT NULL CHECK (kind IN ('training','sick')),
    name                     TEXT NOT NULL,
    start_at                 INTEGER NOT NULL,
    end_at                   INTEGER NOT NULL,
    block_code               TEXT NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN
                                ('scheduled','completed','missed','cancelled')),
    training_reservation_id  INTEGER REFERENCES _training_reservations(id) ON DELETE RESTRICT,
    technician_absence_id    INTEGER REFERENCES _technician_absences(id) ON DELETE RESTRICT,
    created_at               INTEGER NOT NULL
);

CREATE TABLE _on_call_rotations (
    id                    INTEGER PRIMARY KEY,
    business_unit_id      INTEGER NOT NULL REFERENCES _business_units(id) ON DELETE RESTRICT,
    technician_id         INTEGER REFERENCES _technicians(id) ON DELETE RESTRICT,
    effective_start_date  TEXT NOT NULL,
    effective_end_date    TEXT,
    status                TEXT NOT NULL CHECK (status IN ('active','vacant','ended')),
    vacated_at            INTEGER,
    created_at            INTEGER NOT NULL,
    modified_at           INTEGER NOT NULL,
    origin                TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    CHECK (status != 'vacant'
           OR (technician_id IS NULL AND vacated_at IS NOT NULL))
);
-- spec 02 §6.3: no two live rotations per business unit sharing a start date.
CREATE UNIQUE INDEX _on_call_rotations_live
    ON _on_call_rotations (business_unit_id, effective_start_date)
    WHERE status IN ('active','vacant');

-- spec 04 §1.5: hidden true skill, keyed per technician per qualification.
CREATE TABLE _technician_true_skill (
    technician_id INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    skill_code    TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    base_skill    REAL NOT NULL,
    training_gain REAL NOT NULL CHECK (training_gain >= 0),
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (technician_id, skill_code)
);

-- spec 04 §1.5: hidden strain, one row per technician.
CREATE TABLE _technician_strain (
    technician_id     INTEGER PRIMARY KEY REFERENCES _technicians(id) ON DELETE RESTRICT,
    strain_value      REAL NOT NULL CHECK (strain_value BETWEEN 0 AND 1),
    last_updated_date TEXT NOT NULL
);

-- §4.6 Hiring and training -------------------------------------------------
CREATE TABLE _hiring_requests (
    id                          INTEGER PRIMARY KEY,
    requested_at                INTEGER NOT NULL,
    requested_skill_codes_json  TEXT NOT NULL,
    minimum_experience_years    INTEGER NOT NULL,
    preferred_home_zone_id      INTEGER REFERENCES _zones(id) ON DELETE RESTRICT,
    status                      TEXT NOT NULL CHECK (status IN ('open','completed','cancelled')),
    recruiting_cost_cents       INTEGER NOT NULL,
    committed_hourly_wage_cents INTEGER NOT NULL,
    committed_start_date        TEXT NOT NULL,
    technician_id               INTEGER REFERENCES _technicians(id) ON DELETE RESTRICT,
    created_at                  INTEGER NOT NULL,
    modified_at                 INTEGER NOT NULL
);

CREATE TABLE _training_rates (
    id                 INTEGER PRIMARY KEY,
    skill_code         TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    rate_date          TEXT NOT NULL,
    hourly_rate_cents  INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    UNIQUE (skill_code, rate_date)
);

CREATE TABLE _training_reservations (
    id                       INTEGER PRIMARY KEY,
    technician_id            INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    skill_code               TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    scheduled_at             INTEGER NOT NULL,
    total_hours              INTEGER NOT NULL,
    locked_hourly_rate_cents INTEGER NOT NULL,
    prepaid_cost_cents       INTEGER NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN
                                ('scheduled','in_progress','completed',
                                 'partially_completed','cancelled')),
    completed_hours          INTEGER NOT NULL,
    created_at               INTEGER NOT NULL,
    modified_at              INTEGER NOT NULL
);

-- §4.7 Money ----------------------------------------------------------------
CREATE TABLE _invoices (
    id                              INTEGER PRIMARY KEY,
    invoice_number                  TEXT NOT NULL UNIQUE,
    job_id                          INTEGER NOT NULL REFERENCES _jobs(id) ON DELETE RESTRICT,
    customer_id                     INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    location_id                     INTEGER NOT NULL REFERENCES _locations(id) ON DELETE RESTRICT,
    business_unit_id                INTEGER REFERENCES _business_units(id) ON DELETE RESTRICT,
    invoice_date                    TEXT NOT NULL,
    issued_at                       INTEGER NOT NULL,
    status                          TEXT NOT NULL CHECK (status IN ('paid','no_charge')),
    no_charge                       INTEGER NOT NULL CHECK (no_charge IN (0,1)),
    subtotal_cents                  INTEGER NOT NULL,
    total_cents                     INTEGER NOT NULL,
    material_cost_cents             INTEGER NOT NULL,
    material_percent_basis_points   INTEGER NOT NULL,
    created_at                      INTEGER NOT NULL,
    origin                          TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    UNIQUE (job_id),
    CHECK (total_cents >= 0),
    CHECK (no_charge = 0 OR total_cents = 0)
);

CREATE TABLE _invoice_items (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES _invoices(id) ON DELETE RESTRICT,
    item_type    TEXT NOT NULL CHECK (item_type IN ('service')),
    description  TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE TABLE _payments (
    id             INTEGER PRIMARY KEY,
    payment_number TEXT NOT NULL UNIQUE,
    invoice_id     INTEGER NOT NULL REFERENCES _invoices(id) ON DELETE RESTRICT,
    customer_id    INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    paid_at        INTEGER NOT NULL,
    amount_cents   INTEGER NOT NULL,
    payment_type   TEXT NOT NULL CHECK (payment_type IN ('immediate')),
    status         TEXT NOT NULL CHECK (status IN ('posted')),
    created_at     INTEGER NOT NULL,
    origin         TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    UNIQUE (invoice_id)
);

-- spec 02 §4.7 / spec 06 §9.1: the closed 9-member entry-type set, copied
-- verbatim. Append-only (§1.4).
CREATE TABLE _ledger_entries (
    id                       INTEGER PRIMARY KEY,
    occurred_at              INTEGER NOT NULL,
    service_date             TEXT NOT NULL,
    entry_type               TEXT NOT NULL CHECK (entry_type IN
                                ('opening_balance_adjustment','revenue','material_cost',
                                 'payroll_base','payroll_off_hours','payroll_off_hours_bonus',
                                 'recruiting_cost','training_cost','operating_overhead')),
    amount_cents             INTEGER NOT NULL,
    balance_after_cents      INTEGER NOT NULL,
    job_id                   INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    invoice_id               INTEGER REFERENCES _invoices(id) ON DELETE RESTRICT,
    payment_id               INTEGER REFERENCES _payments(id) ON DELETE RESTRICT,
    technician_id            INTEGER REFERENCES _technicians(id) ON DELETE RESTRICT,
    hiring_request_id        INTEGER REFERENCES _hiring_requests(id) ON DELETE RESTRICT,
    training_reservation_id  INTEGER REFERENCES _training_reservations(id) ON DELETE RESTRICT,
    description              TEXT NOT NULL,
    created_at               INTEGER NOT NULL,
    origin                   TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

-- §4.8 Reputation and alerts ------------------------------------------------
-- §1.4 append-only; only sampled, surfaced reviews live here.
CREATE TABLE _reviews (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER REFERENCES _jobs(id) ON DELETE RESTRICT,
    appointment_id  INTEGER REFERENCES _appointments(id) ON DELETE RESTRICT,
    customer_id     INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    technician_id   INTEGER REFERENCES _technicians(id) ON DELETE RESTRICT,
    published_at    INTEGER NOT NULL,
    service_date    TEXT NOT NULL,
    stars           INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    source          TEXT NOT NULL CHECK (source IN ('google')),
    created_at      INTEGER NOT NULL,
    origin          TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
);

-- §1.4 append-only; alert layer over authoritative tables, never the source of
-- truth. notification_type membership is owned by spec 06 / spec 07.
CREATE TABLE _notifications (
    id                 INTEGER PRIMARY KEY,
    created_at         INTEGER NOT NULL,
    service_date       TEXT NOT NULL,
    notification_type  TEXT NOT NULL,
    entity_type        TEXT NOT NULL,
    entity_id          INTEGER NOT NULL,
    message            TEXT NOT NULL
);

-- ============================================================================
-- D. Structural guardrails on _appointment_blocks (decision 14)
-- ============================================================================
-- SQLite forbids subqueries inside CHECK (verified on 3.51.3), so the two
-- cross-table rules of spec 02 §6.4 become BEFORE INSERT triggers with stable
-- sentinel messages the engine maps to structured errors.

-- Block count must not exceed the job type's scheduled_blocks (§6.4):
CREATE TRIGGER trg_appointment_blocks_overflow
BEFORE INSERT ON _appointment_blocks
FOR EACH ROW
WHEN (
    SELECT COUNT(*) FROM _appointment_blocks
    WHERE appointment_id = NEW.appointment_id
) >= (
    SELECT jt.scheduled_blocks
    FROM _job_types jt JOIN _jobs j ON j.job_type_id = jt.id
    WHERE j.id = (SELECT job_id FROM _appointments WHERE id = NEW.appointment_id)
)
BEGIN
    SELECT RAISE(ABORT, 'appointment_block_overflow');
END;

-- An ordinal-1 block must start at the appointment's scheduled_start (§6.4):
CREATE TRIGGER trg_appointment_blocks_ordinal_start
BEFORE INSERT ON _appointment_blocks
FOR EACH ROW
WHEN NEW.ordinal = 1 AND NEW.block_start != (
    SELECT scheduled_start FROM _appointments WHERE id = NEW.appointment_id
)
BEGIN
    SELECT RAISE(ABORT, 'appointment_block_start_mismatch');
END;

-- ============================================================================
-- E. Append-only guard triggers (decision 15)
-- ============================================================================
-- Permanent UPDATE/DELETE guards on the six §1.4 append-only tables other than
-- _events, which is the spec-01-vs-spec-02-§1.4 carve-out: only the events
-- module may update _events, and only its status/applied_at_ms columns.

CREATE TRIGGER trg_ledger_entries_no_update
BEFORE UPDATE ON _ledger_entries
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_ledger_entries_no_delete
BEFORE DELETE ON _ledger_entries
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TRIGGER trg_notifications_no_update
BEFORE UPDATE ON _notifications
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_notifications_no_delete
BEFORE DELETE ON _notifications
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TRIGGER trg_call_attempts_no_update
BEFORE UPDATE ON _call_attempts
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_call_attempts_no_delete
BEFORE DELETE ON _call_attempts
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TRIGGER trg_reviews_no_update
BEFORE UPDATE ON _reviews
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_reviews_no_delete
BEFORE DELETE ON _reviews
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TRIGGER trg_random_audit_no_update
BEFORE UPDATE ON _random_audit
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_random_audit_no_delete
BEFORE DELETE ON _random_audit
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;

CREATE TRIGGER trg_appointment_events_no_update
BEFORE UPDATE ON _appointment_events
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
CREATE TRIGGER trg_appointment_events_no_delete
BEFORE DELETE ON _appointment_events
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END;
