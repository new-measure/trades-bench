from __future__ import annotations

import re
import sqlite3

from . import calls, demand, geography
from .config import RunConfig
from .errors import EngineError, ErrorCode
from .geo_reader import GeoArtifact
from .txn import Services, TxnContext, command

_SEGMENTS = frozenset({"residential", "commercial", "apartment"})
_CONTACT_TYPES = frozenset({"phone", "email"})
_CONTACT_REQUIRED_KEYS = frozenset({"contact_type", "value"})
_CONTACT_OPTIONAL_KEYS = frozenset({"do_not_text", "memo"})
_CONTACT_ALLOWED_KEYS = _CONTACT_REQUIRED_KEYS | _CONTACT_OPTIONAL_KEYS

_PHONE_STRIP_RE = re.compile(r"[\s\-.()]")
_PHONE_RE = re.compile(r"^(?:\+1|1)?(\d{10})$")


def _bad_argument(message: str) -> EngineError:
    return EngineError(ErrorCode.INVALID_ARGUMENT, message)


def _state_illegal(reason: str, message: str, **details: object) -> EngineError:
    return EngineError(ErrorCode.STATE_ILLEGAL, message, details={"reason": reason, **details})


def _is_valid_phone(value: str) -> bool:
    stripped = _PHONE_STRIP_RE.sub("", value)
    match = _PHONE_RE.match(stripped)
    if match is None:
        return False
    digits = match.group(1)
    return digits[0] in "23456789" and digits[3] in "23456789"


def _validate_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise _bad_argument(f"name must be a non-empty string, got {name!r}")
    return name


def _validate_segment(segment: object) -> str:
    if segment not in _SEGMENTS:
        raise _bad_argument(f"segment must be one of {sorted(_SEGMENTS)}, got {segment!r}")
    return segment


def _validate_contact(contact: object) -> dict:
    if not isinstance(contact, dict):
        raise _bad_argument(f"each contact must be a dict, got {contact!r}")
    unknown = set(contact) - _CONTACT_ALLOWED_KEYS
    if unknown:
        raise _bad_argument(f"contact has unknown key(s): {sorted(unknown)}")
    missing = _CONTACT_REQUIRED_KEYS - set(contact)
    if missing:
        raise _bad_argument(f"contact is missing required key(s): {sorted(missing)}")
    contact_type = contact["contact_type"]
    if contact_type not in _CONTACT_TYPES:
        raise _bad_argument(
            f"contact_type must be one of {sorted(_CONTACT_TYPES)}, got {contact_type!r}"
        )
    value = contact["value"]
    if not isinstance(value, str) or not value.strip():
        raise _bad_argument(f"contact value must be a non-empty string, got {value!r}")
    if contact_type == "phone" and not _is_valid_phone(value):
        raise _bad_argument(f"contact value is not a valid North American phone number: {value!r}")
    do_not_text = contact.get("do_not_text", False)
    if not isinstance(do_not_text, bool):
        raise _bad_argument(f"contact do_not_text must be a bool, got {do_not_text!r}")
    memo = contact.get("memo", "")
    if not isinstance(memo, str):
        raise _bad_argument(f"contact memo must be a string, got {memo!r}")
    return {
        "contact_type": contact_type,
        "value": value,
        "do_not_text": do_not_text,
        "memo": memo,
    }


def _validate_contacts(contacts: object, *, require_phone: bool, allow_none: bool) -> list[dict]:
    if contacts is None:
        if allow_none:
            return []
        raise _bad_argument("contacts must be a non-empty list")
    if not isinstance(contacts, (list, tuple)):
        raise _bad_argument(f"contacts must be a list, got {contacts!r}")
    if require_phone and not contacts:
        raise _bad_argument("contacts must include at least one phone contact")
    validated = [_validate_contact(contact) for contact in contacts]
    if require_phone and not any(c["contact_type"] == "phone" for c in validated):
        raise _bad_argument("contacts must include at least one phone contact")
    return validated


def _validate_address(address: object) -> str:
    if not isinstance(address, str) or not address.strip():
        raise _bad_argument(f"address must be a non-empty string, got {address!r}")
    return address


def _call_row(conn: sqlite3.Connection, call_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"call {call_id} not found")
    return row


def _write_contacts(
    conn: sqlite3.Connection, table: str, id_column: str, entity_id: int,
    contacts: list[dict], now: int,
) -> None:
    for contact in contacts:
        conn.execute(
            f"INSERT INTO {table} ({id_column}, contact_type, value, do_not_text, memo, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity_id, contact["contact_type"], contact["value"],
                int(contact["do_not_text"]), contact["memo"], now,
            ),
        )


def create_customer(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    name: str,
    segment: str,
    contacts: object,
) -> dict:
    name = _validate_name(name)
    segment = _validate_segment(segment)
    contacts = _validate_contacts(contacts, require_phone=True, allow_none=False)

    conn = ctx.conn
    now = ctx.now()

    call = _call_row(conn, call_id)
    calls.require_current_new_job_call(ctx, call)
    if call["customer_id"] is not None:
        raise _state_illegal(
            "already_registered", f"call {call_id} already has a registered customer"
        )
    if segment != call["segment"]:
        raise _state_illegal(
            "segment_mismatch",
            f"segment {segment!r} does not match the call's stated segment "
            f"{call['segment']!r}",
        )

    cur = conn.execute(
        "INSERT INTO _customers (name, segment, status, created_at, created_from_call_id, "
        "modified_at) VALUES (?, ?, 'active', ?, ?, ?)",
        (name, segment, now, call_id, now),
    )
    customer_id = int(cur.lastrowid)

    _write_contacts(conn, "_customer_contacts", "customer_id", customer_id, contacts, now)

    conn.execute("UPDATE _calls SET customer_id = ? WHERE id = ?", (customer_id, call_id))

    hidden = conn.execute(
        "SELECT prospect_id FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    if hidden is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, f"call {call_id} has no _call_hidden row"
        )
    prospect_id = hidden["prospect_id"]
    if prospect_id is not None:
        demand.transfer_prospect_to_customer(
            conn, prospect_id=prospect_id, customer_id=customer_id, now_ms=now
        )
    else:
        demand.register_customer_repeat_state(
            conn, config, customer_id=customer_id, segment=segment, now_ms=now
        )

    return {"customer_id": customer_id}


def create_location(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    customer_id: int,
    address: str,
    contacts: object = None,
    artifact: GeoArtifact | None = None,
) -> dict:
    address = _validate_address(address)
    contacts = _validate_contacts(contacts, require_phone=False, allow_none=True)

    conn = ctx.conn
    now = ctx.now()

    call = _call_row(conn, call_id)
    calls.require_current_new_job_call(ctx, call)
    if call["customer_id"] is None:
        raise _state_illegal(
            "customer_not_registered", f"call {call_id} has no registered customer yet"
        )
    if customer_id != call["customer_id"]:
        raise _state_illegal(
            "customer_mismatch",
            f"customer_id {customer_id} does not match the call's customer "
            f"{call['customer_id']}",
        )
    if call["location_id"] is not None:
        raise _state_illegal(
            "already_registered", f"call {call_id} already has a registered location"
        )

    if artifact is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, "geo artifact required for create_location"
        )
    catalog, normalized = geography.resolve_catalog(conn, artifact.abbreviation_table, address)
    if int(catalog["id"]) != call["address_catalog_id"]:
        raise _state_illegal(
            "address_mismatch",
            f"address {address!r} does not match the call's stated address",
        )

    street_line = f"{catalog['house_number']} {catalog['street']}"
    cur = conn.execute(
        "INSERT INTO _locations (customer_id, name, street, unit, city, state, postal_code, "
        "latitude, longitude, zone_id, active, created_at, modified_at, effective_zone_id, "
        "address_catalog_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            customer_id, street_line, street_line, normalized.unit or "",
            catalog["city"], catalog["state"], catalog["postal_code"],
            catalog["latitude"], catalog["longitude"], catalog["zone_id"],
            now, now, catalog["effective_zone_id"], int(catalog["id"]),
        ),
    )
    location_id = int(cur.lastrowid)

    _write_contacts(conn, "_location_contacts", "location_id", location_id, contacts, now)

    conn.execute("UPDATE _calls SET location_id = ? WHERE id = ?", (location_id, call_id))

    return {"location_id": location_id}


def create_customer_command(
    services: Services,
    request_id: str,
    config: RunConfig,
    *,
    call_id: int,
    name: str,
    segment: str,
    contacts: object,
) -> bytes:
    return command(
        services,
        request_id,
        lambda ctx: create_customer(
            ctx, config, call_id=call_id, name=name, segment=segment, contacts=contacts,
        ),
        operation="crm.create_customer",
        args={"call_id": call_id, "name": name, "segment": segment, "contacts": contacts},
    )


def create_location_command(
    services: Services,
    request_id: str,
    config: RunConfig,
    *,
    call_id: int,
    customer_id: int,
    address: str,
    contacts: object = None,
    artifact: GeoArtifact | None = None,
) -> bytes:
    return command(
        services,
        request_id,
        lambda ctx: create_location(
            ctx, config, call_id=call_id, customer_id=customer_id, address=address,
            contacts=contacts, artifact=artifact,
        ),
        operation="crm.create_location",
        args={"call_id": call_id, "customer_id": customer_id, "address": address, "contacts": contacts},
    )


__all__ = [
    "create_customer",
    "create_customer_command",
    "create_location",
    "create_location_command",
]
