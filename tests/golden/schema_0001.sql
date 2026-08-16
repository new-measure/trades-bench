-- Golden schema dump of the migrated CSR-Bench database.
--
-- Regenerate deliberately with:
--   REGEN_GOLDEN_SCHEMA=1 uv run pytest tests/test_schema_contract.py -q
--
-- Format: the canonical sqlite_master dump of a freshly migrated database.
-- Entries are ordered (type groups table / index / trigger, names sorted
-- within a type) and each carries its stored SQL text verbatim; objects
-- without stored SQL text (SQLite's implicit auto-indexes for UNIQUE
-- constraints) are marked. Views are excluded because migrations never
-- create them. A schema change makes the compare test fail; regenerating
-- the file is the deliberate acceptance of the new schema.
-- table: _acceptance_parameters
CREATE TABLE _acceptance_parameters (
    urgency_class TEXT PRIMARY KEY CHECK (urgency_class IN ('standard','urgent')),
    base_micros   INTEGER NOT NULL,
    decay_micros  INTEGER NOT NULL,
    floor_micros  INTEGER NOT NULL,
    created_at    INTEGER NOT NULL
)

-- table: _address_catalog
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
)

-- table: _appointment_assignments
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
)

-- table: _appointment_blocks
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
)

-- table: _appointment_events
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
)

-- table: _appointments
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
, lateness_minutes INTEGER)

-- table: _block_locks
CREATE TABLE _block_locks (
    service_date TEXT NOT NULL,
    block_code   TEXT NOT NULL CHECK (block_code IN
                   ('B07_09','B09_11','B11_13','B13_15','B15_17','B17_19')),
    block_start  INTEGER NOT NULL,
    locked_at    INTEGER NOT NULL,
    PRIMARY KEY (service_date, block_code)
)

-- table: _business_units
CREATE TABLE _business_units (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at  INTEGER NOT NULL
)

-- table: _call_attempts
CREATE TABLE _call_attempts (
    id             INTEGER PRIMARY KEY,
    call_id        INTEGER NOT NULL REFERENCES _calls(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL,
    prompted_at    INTEGER NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('incomplete','resolved')),
    created_at     INTEGER NOT NULL
)

-- table: _call_hidden
CREATE TABLE _call_hidden (
    call_id                 INTEGER PRIMARY KEY REFERENCES _calls(id) ON DELETE RESTRICT,
    true_segment            TEXT NOT NULL CHECK (true_segment IN
                              ('residential','commercial','apartment')),
    requested_category_id   INTEGER NOT NULL REFERENCES _service_catalog(id) ON DELETE RESTRICT,
    prospect_id             INTEGER REFERENCES _prospect_state(id) ON DELETE RESTRICT,
    acceptance_result       INTEGER,
    created_at              INTEGER NOT NULL
, arrival_event_id INTEGER
    REFERENCES _events(event_id) ON DELETE RESTRICT)

-- table: _calls
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
)

-- table: _cancellation_schedule
CREATE TABLE _cancellation_schedule (
    appointment_id INTEGER PRIMARY KEY REFERENCES _appointments(id) ON DELETE RESTRICT,
    cancel_exists  INTEGER NOT NULL CHECK (cancel_exists IN (0,1)),
    arrival_at     INTEGER,
    event_id       INTEGER REFERENCES _events(event_id) ON DELETE RESTRICT,
    created_at     INTEGER NOT NULL
)

-- table: _clock
CREATE TABLE _clock (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_time_ms INTEGER NOT NULL,
    service_date    TEXT NOT NULL
)

-- table: _competitor_state
CREATE TABLE _competitor_state (
    singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
    share_micros  INTEGER NOT NULL,
    capture_count INTEGER NOT NULL,
    drift_micros  INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
)

-- table: _config
CREATE TABLE _config (
    run_id          TEXT PRIMARY KEY REFERENCES _run(run_id) ON DELETE CASCADE,
    config_version  TEXT NOT NULL,
    canonical_json  TEXT NOT NULL
)

-- table: _customer_contacts
CREATE TABLE _customer_contacts (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES _customers(id) ON DELETE RESTRICT,
    contact_type TEXT NOT NULL CHECK (contact_type IN ('phone','email')),
    value        TEXT NOT NULL,
    do_not_text  INTEGER NOT NULL CHECK (do_not_text IN (0,1)),
    memo         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
)

-- table: _customer_repeat_state
CREATE TABLE _customer_repeat_state (
    customer_id            INTEGER PRIMARY KEY REFERENCES _customers(id) ON DELETE RESTRICT,
    propensity_micros       INTEGER NOT NULL,
    last_outcome             TEXT,
    competitor_captured_at   INTEGER,
    updated_at               INTEGER NOT NULL
)

-- table: _customers
CREATE TABLE _customers (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    segment              TEXT NOT NULL CHECK (segment IN ('commercial','apartment','residential')),
    status               TEXT NOT NULL CHECK (status IN ('active','inactive')),
    created_at           INTEGER NOT NULL,
    created_from_call_id INTEGER REFERENCES _calls(id) ON DELETE RESTRICT,
    modified_at          INTEGER NOT NULL,
    origin               TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
)

-- table: _dataset_versions
CREATE TABLE _dataset_versions (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    version        TEXT NOT NULL,
    snapshot_date  TEXT,
    checksum       TEXT NOT NULL,
    attribution    TEXT,
    created_at     INTEGER NOT NULL
)

-- table: _day_state
CREATE TABLE _day_state (
    service_date                 TEXT PRIMARY KEY,
    day_type                     TEXT NOT NULL CHECK (day_type IN ('weekday','weekend')),
    opening_at                   INTEGER,
    closing_at                   INTEGER,
    settlement_status            TEXT NOT NULL DEFAULT 'pending'
                                   CHECK (settlement_status IN ('pending','settled')),
    unresolved_execution_count   INTEGER NOT NULL DEFAULT 0
                                   CHECK (unresolved_execution_count >= 0),
    last_execution_completed_at  INTEGER
)

-- table: _events
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
)

-- table: _hiring_requests
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
)

-- table: _idempotency
CREATE TABLE _idempotency (
    namespace       TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    response        BLOB NOT NULL,
    created_at_ms   INTEGER NOT NULL, fingerprint TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (namespace, request_id)
)

-- table: _invocations
CREATE TABLE _invocations (
    invocation_id   INTEGER PRIMARY KEY,
    type            TEXT NOT NULL,
    ts_ms           INTEGER NOT NULL,
    sequence        INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active','completed')),
    prompt_version  TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0
)

-- table: _invoice_items
CREATE TABLE _invoice_items (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES _invoices(id) ON DELETE RESTRICT,
    item_type    TEXT NOT NULL CHECK (item_type IN ('service')),
    description  TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
)

-- table: _invoices
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
)

-- table: _job_types
CREATE TABLE _job_types (
    id                  INTEGER PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    business_unit_id    INTEGER REFERENCES _business_units(id) ON DELETE RESTRICT,
    scheduled_blocks    INTEGER NOT NULL,
    required_skill_code TEXT REFERENCES _skills(code) ON DELETE RESTRICT,
    active              INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at          INTEGER NOT NULL
)

-- table: _jobs
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
)

-- table: _ledger_entries
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
)

-- table: _location_contacts
CREATE TABLE _location_contacts (
    id           INTEGER PRIMARY KEY,
    location_id  INTEGER NOT NULL REFERENCES _locations(id) ON DELETE RESTRICT,
    contact_type TEXT NOT NULL CHECK (contact_type IN ('phone','email')),
    value        TEXT NOT NULL,
    do_not_text  INTEGER NOT NULL CHECK (do_not_text IN (0,1)),
    memo         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run'
)

-- table: _locations
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
)

-- table: _non_job_appointments
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
)

-- table: _notifications
CREATE TABLE _notifications (
    id                 INTEGER PRIMARY KEY,
    created_at         INTEGER NOT NULL,
    service_date       TEXT NOT NULL,
    notification_type  TEXT NOT NULL,
    entity_type        TEXT NOT NULL,
    entity_id          INTEGER NOT NULL,
    message            TEXT NOT NULL
)

-- table: _on_call_rotations
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
)

-- table: _payments
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
)

-- table: _pending_calls
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
)

-- table: _prospect_state
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
)

-- table: _random_audit
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
)

-- table: _reviews
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
)

-- table: _rng_state
CREATE TABLE _rng_state (
    stream_name TEXT PRIMARY KEY,
    seed        TEXT NOT NULL,
    counter     INTEGER NOT NULL
)

-- table: _routing_anchors
CREATE TABLE _routing_anchors (
    anchor_id       INTEGER PRIMARY KEY,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    selection_cell  TEXT NOT NULL
)

-- table: _run
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
)

-- table: _schema_migrations
CREATE TABLE _schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  INTEGER NOT NULL
)

-- table: _service_catalog
CREATE TABLE _service_catalog (
    id                    INTEGER PRIMARY KEY,
    catalog_version       TEXT NOT NULL,
    name                  TEXT NOT NULL,
    supported_job_type_id INTEGER REFERENCES _job_types(id) ON DELETE RESTRICT,
    demand_weight_key     TEXT NOT NULL,
    active                INTEGER NOT NULL CHECK (active IN (0,1))
)

-- table: _skills
CREATE TABLE _skills (
    id                INTEGER PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    business_unit_id  INTEGER NOT NULL REFERENCES _business_units(id) ON DELETE RESTRICT,
    active            INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at        INTEGER NOT NULL
)

-- table: _technician_absences
CREATE TABLE _technician_absences (
    id                    INTEGER PRIMARY KEY,
    technician_id         INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    absence_kind          TEXT NOT NULL CHECK (absence_kind IN ('sick')),
    start_date            TEXT NOT NULL,
    expected_return_date  TEXT NOT NULL,
    actual_return_date    TEXT,
    status                TEXT NOT NULL CHECK (status IN ('active','ended')),
    created_at            INTEGER NOT NULL
)

-- table: _technician_block_holds
CREATE TABLE _technician_block_holds (
    technician_id           INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    block_start             INTEGER NOT NULL,
    hold_kind               TEXT NOT NULL CHECK (hold_kind IN ('job','training','absence')),
    appointment_id          INTEGER REFERENCES _appointments(id) ON DELETE RESTRICT,
    non_job_appointment_id  INTEGER REFERENCES _non_job_appointments(id) ON DELETE RESTRICT,
    created_at              INTEGER NOT NULL,
    PRIMARY KEY (technician_id, block_start),
    CHECK ((appointment_id IS NULL) != (non_job_appointment_id IS NULL))
)

-- table: _technician_locations
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
)

-- table: _technician_movements
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
)

-- table: _technician_shifts
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
)

-- table: _technician_skills
CREATE TABLE _technician_skills (
    id             INTEGER PRIMARY KEY,
    technician_id  INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    skill_code     TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    active         INTEGER NOT NULL CHECK (active IN (0,1)),
    granted_on     TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    origin         TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run',
    UNIQUE (technician_id, skill_code)
)

-- table: _technician_strain
CREATE TABLE _technician_strain (
    technician_id     INTEGER PRIMARY KEY REFERENCES _technicians(id) ON DELETE RESTRICT,
    strain_value      REAL NOT NULL CHECK (strain_value BETWEEN 0 AND 1),
    last_updated_date TEXT NOT NULL
)

-- table: _technician_true_skill
CREATE TABLE _technician_true_skill (
    technician_id INTEGER NOT NULL REFERENCES _technicians(id) ON DELETE RESTRICT,
    skill_code    TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    base_skill    REAL NOT NULL,
    training_gain REAL NOT NULL CHECK (training_gain >= 0),
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (technician_id, skill_code)
)

-- table: _technicians
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
    origin                TEXT NOT NULL CHECK (origin IN ('warm_start','run')) DEFAULT 'run', home_address_catalog_id INTEGER
    REFERENCES _address_catalog(id) ON DELETE RESTRICT,
    CHECK (employment_status NOT IN ('terminated','resigned')
           OR final_working_date IS NOT NULL)
)

-- table: _training_rates
CREATE TABLE _training_rates (
    id                 INTEGER PRIMARY KEY,
    skill_code         TEXT NOT NULL REFERENCES _skills(code) ON DELETE RESTRICT,
    rate_date          TEXT NOT NULL,
    hourly_rate_cents  INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    UNIQUE (skill_code, rate_date)
)

-- table: _training_reservations
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
)

-- table: _visibility_registry
CREATE TABLE _visibility_registry (
    "table"          TEXT NOT NULL,
    "column"         TEXT NOT NULL,
    visibility_class TEXT NOT NULL,
    PRIMARY KEY ("table", "column")
)

-- table: _weather_forecast_cache
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
)

-- table: _weather_process_state
CREATE TABLE _weather_process_state (
    singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
    seasonal_amplitude_f    INTEGER NOT NULL,
    seasonal_phase_days     INTEGER NOT NULL,
    final_x_milli           INTEGER NOT NULL,
    precompute_start_date   TEXT NOT NULL,
    precompute_end_date     TEXT NOT NULL,
    created_at              INTEGER NOT NULL
)

-- table: _word_of_mouth_state
CREATE TABLE _word_of_mouth_state (
    segment    TEXT PRIMARY KEY CHECK (segment IN ('residential','commercial','apartment')),
    wom_micros INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)

-- table: _zone_geometry
CREATE TABLE _zone_geometry (
    zone_id         INTEGER PRIMARY KEY REFERENCES _zones(id) ON DELETE RESTRICT,
    centroid_lat    REAL NOT NULL,
    centroid_lon    REAL NOT NULL,
    boundary_ref    TEXT
)

-- table: _zone_weather_daily
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
)

-- table: _zones
CREATE TABLE _zones (
    id                 INTEGER PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    cities_json        TEXT NOT NULL,
    postal_codes_json  TEXT NOT NULL,
    active             INTEGER NOT NULL CHECK (active IN (0,1)),
    created_at         INTEGER NOT NULL
)

-- index: _appointment_assignments_active
CREATE UNIQUE INDEX _appointment_assignments_active
    ON _appointment_assignments (appointment_id) WHERE status = 'active'

-- index: _events_order
CREATE INDEX _events_order ON _events (scheduled_at_ms, priority, event_id)

-- index: _events_status
CREATE INDEX _events_status ON _events (status)

-- index: _invocations_order
CREATE INDEX _invocations_order ON _invocations (sequence)

-- index: _on_call_rotations_live
CREATE UNIQUE INDEX _on_call_rotations_live
    ON _on_call_rotations (business_unit_id, effective_start_date)
    WHERE status IN ('active','vacant')

-- index: _pending_calls_order
CREATE INDEX _pending_calls_order ON _pending_calls (service_date, arrival_at)

-- index: _random_audit_key
CREATE INDEX _random_audit_key ON _random_audit
    (stream_name, entity_type, entity_id, scope, event_kind, draw_index)

-- trigger: trg_appointment_blocks_ordinal_start
CREATE TRIGGER trg_appointment_blocks_ordinal_start
BEFORE INSERT ON _appointment_blocks
FOR EACH ROW
WHEN NEW.ordinal = 1 AND NEW.block_start != (
    SELECT scheduled_start FROM _appointments WHERE id = NEW.appointment_id
)
BEGIN
    SELECT RAISE(ABORT, 'appointment_block_start_mismatch');
END

-- trigger: trg_appointment_blocks_overflow
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
END

-- trigger: trg_appointment_events_no_delete
CREATE TRIGGER trg_appointment_events_no_delete
BEFORE DELETE ON _appointment_events
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_appointment_events_no_update
CREATE TRIGGER trg_appointment_events_no_update
BEFORE UPDATE ON _appointment_events
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_block_locks_no_delete
CREATE TRIGGER trg_block_locks_no_delete
BEFORE DELETE ON _block_locks
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_block_locks_no_update
CREATE TRIGGER trg_block_locks_no_update
BEFORE UPDATE ON _block_locks
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_call_attempts_no_delete
CREATE TRIGGER trg_call_attempts_no_delete
BEFORE DELETE ON _call_attempts
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_call_attempts_no_update
CREATE TRIGGER trg_call_attempts_no_update
BEFORE UPDATE ON _call_attempts
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_ledger_entries_no_delete
CREATE TRIGGER trg_ledger_entries_no_delete
BEFORE DELETE ON _ledger_entries
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_ledger_entries_no_update
CREATE TRIGGER trg_ledger_entries_no_update
BEFORE UPDATE ON _ledger_entries
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_notifications_no_delete
CREATE TRIGGER trg_notifications_no_delete
BEFORE DELETE ON _notifications
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_notifications_no_update
CREATE TRIGGER trg_notifications_no_update
BEFORE UPDATE ON _notifications
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_random_audit_no_delete
CREATE TRIGGER trg_random_audit_no_delete
BEFORE DELETE ON _random_audit
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_random_audit_no_update
CREATE TRIGGER trg_random_audit_no_update
BEFORE UPDATE ON _random_audit
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_reviews_no_delete
CREATE TRIGGER trg_reviews_no_delete
BEFORE DELETE ON _reviews
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_reviews_no_update
CREATE TRIGGER trg_reviews_no_update
BEFORE UPDATE ON _reviews
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_weather_forecast_cache_no_delete
CREATE TRIGGER trg_weather_forecast_cache_no_delete
BEFORE DELETE ON _weather_forecast_cache
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_weather_forecast_cache_no_update
CREATE TRIGGER trg_weather_forecast_cache_no_update
BEFORE UPDATE ON _weather_forecast_cache
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_zone_weather_daily_no_delete
CREATE TRIGGER trg_zone_weather_daily_no_delete
BEFORE DELETE ON _zone_weather_daily
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END

-- trigger: trg_zone_weather_daily_no_update
CREATE TRIGGER trg_zone_weather_daily_no_update
BEFORE UPDATE ON _zone_weather_daily
FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'append_only_table'); END
