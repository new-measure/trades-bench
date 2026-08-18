from __future__ import annotations

import base64
import sqlite3
from datetime import date

from . import clock, geography, weather
from .config import RunConfig
from .errors import EngineError, ErrorCode
from .geo_reader import GeoArtifact
from .registry import _VIEW_COLUMNS, OperationSpec, _filter_predicate
from .rng import Rng

_CURSOR_PK_MAX = (1 << 63) - 1


def _validation_error(message: str, *, rule: str | None = None, field: str | None = None) -> EngineError:
    details: dict[str, object] = {}
    if rule is not None:
        details["rule"] = rule
    if field is not None:
        details["field"] = field
    return EngineError(ErrorCode.INVALID_ARGUMENT, message, details=details or None)


def _parse_date(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise _validation_error(f"{name} must be a YYYY-MM-DD string", field=name)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _validation_error(f"{name} must be a YYYY-MM-DD string", field=name) from exc
    if parsed.isoformat() != value:
        raise _validation_error(f"{name} must be a YYYY-MM-DD string", field=name)
    return value


_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _coerce_arg(name: str, type: str, value: object) -> object:
    if type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be an integer")
        if value < _INT64_MIN or value > _INT64_MAX:
            raise EngineError(
                ErrorCode.INVALID_ARGUMENT,
                f"{name} is out of range",
                details={"field": name},
            )
        return value
    if type == "str":
        if not isinstance(value, str):
            raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be a string")
        return value
    if type == "date":
        return _parse_date(name, value)
    if type == "bool":
        if not isinstance(value, bool):
            raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be a boolean")
        return value
    if type == "list":
        if not isinstance(value, list):
            raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be a list")
        return value
    if type == "object":
        if value is None or not isinstance(value, (dict, list)):
            raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be a JSON object or list")
        return value
    raise AssertionError(f"unsupported arg type {type!r}")


def validate_operation_args(spec: OperationSpec, body: dict) -> dict[str, object]:
    allowed = {arg.name for arg in spec.args}
    if spec.read is not None:
        allowed |= {flt.arg_name for flt in spec.read.filters}
    unknown = set(body) - allowed
    if unknown:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"unknown argument(s): {sorted(unknown)}",
        )
    validated: dict[str, object] = {}
    for arg in spec.args:
        if arg.name in body:
            validated[arg.name] = _coerce_arg(arg.name, arg.type, body[arg.name])
        elif arg.required:
            raise EngineError(
                ErrorCode.INVALID_ARGUMENT,
                f"missing required argument {arg.name!r}",
                details={"field": arg.name},
            )
    if spec.read is not None:
        for flt in spec.read.filters:
            if flt.required and flt.arg_name not in validated:
                raise EngineError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"missing required argument {flt.arg_name!r}",
                    details={"field": flt.arg_name},
                )
    return validated


def decode_cursor(raw: str) -> int:
    if not isinstance(raw, str) or not raw:
        raise _validation_error("cursor must be a non-empty string", field="cursor")
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(raw + padding, altchars=b"-_", validate=True)
        text = decoded.decode("ascii")
        value = int(text)
    except (ValueError, UnicodeDecodeError, OverflowError) as exc:
        raise _validation_error("cursor is malformed", field="cursor") from exc
    if value < 0 or value > _CURSOR_PK_MAX:
        raise _validation_error("cursor is malformed", field="cursor")
    if encode_cursor(value) != raw:
        raise _validation_error("cursor is malformed", field="cursor")
    return value


def encode_cursor(pk: int) -> str:
    return base64.urlsafe_b64encode(str(pk).encode("ascii")).decode("ascii").rstrip("=")


def _row_to_dict(columns: tuple[str, ...], row: sqlite3.Row) -> dict[str, object]:
    out: dict[str, object] = {}
    for index, name in enumerate(columns):
        value = row[index]
        if isinstance(value, bytes):
            value = value.hex()
        out[name] = value
    return out


def _validate_limit(requested: int | None) -> None:
    if requested is None:
        return
    if not isinstance(requested, int) or isinstance(requested, bool):
        raise EngineError(ErrorCode.INVALID_ARGUMENT, "limit must be an integer")
    if requested <= 0:
        raise EngineError(ErrorCode.INVALID_ARGUMENT, "limit must be positive")


def _effective_limit(config: RunConfig, requested: int | None) -> int:
    default_limit = config.get_int("interface.list_default_page_size")
    max_limit = config.get_int("interface.list_max_page_size")
    limit = default_limit if requested is None else requested
    return min(max_limit, limit)


def build_view_list_sql(
    spec: OperationSpec,
    args: dict[str, object],
    *,
    limit: int | None = None,
) -> tuple[str, list[object]]:
    read = spec.read
    assert read is not None and read.view is not None and read.pk_column is not None
    columns = _VIEW_COLUMNS[read.view]
    pk_column = read.pk_column
    where_parts: list[str] = []
    params: list[object] = []
    for flt in read.filters:
        if flt.arg_name not in args:
            continue
        value = args[flt.arg_name]
        where_parts.append(_filter_predicate(spec, flt))
        params.append(value)

    cursor_raw = args.get("cursor")
    if cursor_raw is not None:
        cursor_pk = decode_cursor(str(cursor_raw))
        where_parts.append(f"{pk_column} > ?")
        params.append(cursor_pk)

    sql = f"SELECT {', '.join(columns)} FROM {read.view}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += f" ORDER BY {pk_column} ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return sql, params


def execute_view_get(
    conn: sqlite3.Connection,
    spec: OperationSpec,
    args: dict[str, object],
) -> dict[str, object]:
    read = spec.read
    assert read is not None and read.view is not None and read.get_id_arg is not None
    columns = _VIEW_COLUMNS[read.view]
    pk_column = read.pk_column or "id"
    pk_value = args[read.get_id_arg]
    sql = f"SELECT {', '.join(columns)} FROM {read.view} WHERE {pk_column} = ?"
    row = conn.execute(sql, (pk_value,)).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"{read.view} row not found for {read.get_id_arg}={pk_value!r}",
        )
    return _row_to_dict(columns, row)


def execute_view_singleton(
    conn: sqlite3.Connection,
    spec: OperationSpec,
) -> dict[str, object]:
    read = spec.read
    assert read is not None and read.view is not None
    columns = read.singleton_fields or _VIEW_COLUMNS[read.view]
    sql = f"SELECT {', '.join(columns)} FROM {read.view}"
    row = conn.execute(sql).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"{read.view} has no row",
        )
    return _row_to_dict(columns, row)


def execute_view_list(
    conn: sqlite3.Connection,
    config: RunConfig,
    spec: OperationSpec,
    args: dict[str, object],
) -> tuple[list[dict[str, object]], str | None]:
    read = spec.read
    assert read is not None and read.view is not None and read.pk_column is not None
    if read.unpaginated:
        return execute_view_list_unpaginated(conn, spec, args), None
    columns = _VIEW_COLUMNS[read.view]
    pk_column = read.pk_column
    requested_limit = args.get("limit")
    _validate_limit(requested_limit)
    limit = _effective_limit(config, requested_limit)
    sql, params = build_view_list_sql(spec, args, limit=limit + 1)
    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    selected = rows[:limit]
    data = [_row_to_dict(columns, row) for row in selected]
    next_cursor = None
    if has_more and selected:
        last_pk = selected[-1][columns.index(pk_column)]
        next_cursor = encode_cursor(int(last_pk))
    return data, next_cursor


def execute_view_list_unpaginated(
    conn: sqlite3.Connection,
    spec: OperationSpec,
    args: dict[str, object],
) -> list[dict[str, object]]:
    read = spec.read
    assert read is not None and read.view is not None and read.pk_column is not None
    columns = _VIEW_COLUMNS[read.view]
    sql, params = build_view_list_sql(spec, args)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(columns, row) for row in rows]


def execute_weather(
    conn: sqlite3.Connection,
    config: RunConfig,
    rng: Rng,
    args: dict[str, object],
) -> dict[str, object]:
    zone_id = int(args["zone_id"])
    target_date = str(args["date"])
    today = clock.now_service_date(conn)
    max_horizon = config.get_int("weather.forecast_max_horizon_days")
    try:
        today_d = date.fromisoformat(today)
        target_d = date.fromisoformat(target_date)
    except ValueError as exc:
        raise EngineError(ErrorCode.INVALID_ARGUMENT, "date must be a YYYY-MM-DD string") from exc
    horizon = (target_d - today_d).days
    if horizon < 0 or horizon > max_horizon:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"date {target_date} is outside the weather horizon for today={today}",
            details={"rule": "date_out_of_horizon"},
        )
    return weather.get_weather(
        conn, config, rng, zone_id=zone_id, date=target_date, today=today
    )


def execute_maps(
    conn: sqlite3.Connection,
    config: RunConfig,
    artifact: GeoArtifact | None,
    args: dict[str, object],
) -> dict[str, object]:
    if artifact is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            "geo artifact required for maps.distance",
        )
    origin = str(args["origin_address"])
    destination = str(args["destination_address"])
    result = geography.maps_distance(conn, artifact, config, origin, destination)
    return {
        "origin_address": result.origin_address,
        "destination_address": result.destination_address,
        "distance_km": float(result.distance_km),
        "distance_miles": float(result.distance_miles),
        "dataset_version": result.dataset_version,
    }


__all__ = [
    "build_view_list_sql",
    "decode_cursor",
    "encode_cursor",
    "execute_maps",
    "execute_view_get",
    "execute_view_list",
    "execute_view_list_unpaginated",
    "execute_view_singleton",
    "execute_weather",
    "validate_operation_args",
]
