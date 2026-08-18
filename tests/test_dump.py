from __future__ import annotations

import math
import sqlite3
import struct

from _helpers import register_conn
from trades_bench._dump import dump_bytes
from trades_bench.db import in_memory, insert_run
from trades_bench.enums import RunStatus
from trades_bench.migrations import migrate

RUN_ID = "run-reals"


def _conn_with_real_row(lat: float, lon: float) -> sqlite3.Connection:
    conn = in_memory()
    register_conn(conn)
    migrate(conn)
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=RUN_ID,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.ACTIVE,
    )
    conn.execute(
        "INSERT INTO _technicians(id, name, street, unit, city, state, postal_code, "
        "home_latitude, home_longitude, joined_on, hourly_wage_cents, "
        "employment_status, availability_status, created_at, modified_at) "
        "VALUES (1, 't', 's', '', 'c', 'AZ', '85001', ?, ?, '2026-10-01', 25000, "
        "'active', 'available', 0, 0)",
        (lat, lon),
    )
    conn.execute(
        "INSERT INTO _technician_locations(technician_id, latitude, longitude, "
        "coordinates_updated_at, location_state) VALUES (1, ?, ?, 0, 'home')",
        (lat, lon),
    )
    conn.execute("COMMIT")
    return conn


def test_dump_serializes_real_columns_and_is_stable():
    conn = _conn_with_real_row(33.448376, -112.074036)

    first = dump_bytes(conn, RUN_ID)
    second = dump_bytes(conn, RUN_ID)
    assert first == second


def test_ulp_difference_changes_the_dump():
    lat = 33.448376
    near = lat + math.ulp(lat)
    assert near != lat
    assert dump_bytes(_conn_with_real_row(lat, 112.0), RUN_ID) != dump_bytes(
        _conn_with_real_row(near, 112.0), RUN_ID
    )


def test_real_serialized_as_bit_exact_float_hex():
    conn = _conn_with_real_row(33.448376, -112.074036)
    row = conn.execute("SELECT latitude FROM _technician_locations").fetchone()
    expected = "float:" + struct.pack(">d", row["latitude"]).hex()
    assert expected in dump_bytes(conn, RUN_ID).decode("utf-8")
