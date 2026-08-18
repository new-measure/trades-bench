from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import address, geo_reader
from .config import RunConfig
from .errors import EngineError, ErrorCode

KM_PER_MILE = Decimal("1.609344")

_ONE_DECIMAL = Decimal("0.1")


@dataclass(frozen=True)
class CatalogPoint:

    address_id: int
    anchor_id: int
    access_distance_km: float
    formatted_address: str
    dataset_version: str


@dataclass(frozen=True)
class MapsDistanceResult:

    origin_address: str
    destination_address: str
    distance_km: Decimal
    distance_miles: Decimal
    dataset_version: str


def resolve_catalog(
    conn: sqlite3.Connection,
    abbreviation_table: Mapping[str, Mapping[str, str]],
    address_text: str,
) -> tuple[sqlite3.Row, address.NormalizedAddress]:
    normalized = address.normalize(address_text, abbreviation_table)
    row = conn.execute(
        "SELECT id, house_number, street, city, state, postal_code, latitude, longitude, "
        "zone_id, effective_zone_id FROM _address_catalog "
        "WHERE canonical_key = ? AND status = 'usable'",
        (normalized.canonical_key,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ADDRESS_UNRESOLVABLE,
            f"no catalog address matches {address_text!r}",
            details={"canonical_key": normalized.canonical_key},
        )
    return row, normalized


def resolve(
    conn: sqlite3.Connection,
    abbreviation_table: Mapping[str, Mapping[str, str]],
    address_text: str,
) -> int:
    row, _ = resolve_catalog(conn, abbreviation_table, address_text)
    return int(row["id"])


def catalog_point(conn: sqlite3.Connection, address_id: int) -> CatalogPoint:
    row = conn.execute(
        "SELECT id, anchor_id, access_distance_km, formatted_address, dataset_version, status "
        "FROM _address_catalog WHERE id = ?",
        (address_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"address_id {address_id} not found in the address catalog",
        )
    if row["status"] != "usable":
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"address_id {address_id} is not usable and carries no routing anchor",
        )
    return CatalogPoint(
        address_id=int(row["id"]),
        anchor_id=int(row["anchor_id"]),
        access_distance_km=float(row["access_distance_km"]),
        formatted_address=row["formatted_address"],
        dataset_version=row["dataset_version"],
    )


def route_distance_km(
    access_a_km: float,
    anchor_a: int,
    access_b_km: float,
    anchor_b: int,
    artifact: geo_reader.GeoArtifact,
    config: RunConfig,
) -> float:
    min_route_distance_km = float(config.get_decimal("geography.min_route_distance_km"))
    intra_cell_detour_factor = float(config.get_decimal("geography.intra_cell_detour_factor"))
    access_leg_sum = access_a_km + access_b_km
    if anchor_a == anchor_b:
        return max(min_route_distance_km, access_leg_sum * intra_cell_detour_factor)
    return access_leg_sum + artifact.anchor_distance_km(anchor_a, anchor_b)


def _round_one_decimal(value: Decimal) -> Decimal:
    return value.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)


def maps_distance(
    conn: sqlite3.Connection,
    artifact: geo_reader.GeoArtifact,
    config: RunConfig,
    origin_address_text: str,
    destination_address_text: str,
) -> MapsDistanceResult:
    origin_id = resolve(conn, artifact.abbreviation_table, origin_address_text)
    destination_id = resolve(conn, artifact.abbreviation_table, destination_address_text)
    origin = catalog_point(conn, origin_id)
    destination = catalog_point(conn, destination_id)
    unrounded_km = route_distance_km(
        origin.access_distance_km,
        origin.anchor_id,
        destination.access_distance_km,
        destination.anchor_id,
        artifact,
        config,
    )
    km_decimal = Decimal(str(unrounded_km))
    return MapsDistanceResult(
        origin_address=origin.formatted_address,
        destination_address=destination.formatted_address,
        distance_km=_round_one_decimal(km_decimal),
        distance_miles=_round_one_decimal(km_decimal / KM_PER_MILE),
        dataset_version=origin.dataset_version,
    )


__all__ = [
    "KM_PER_MILE",
    "CatalogPoint",
    "MapsDistanceResult",
    "catalog_point",
    "maps_distance",
    "resolve",
    "resolve_catalog",
    "route_distance_km",
]
