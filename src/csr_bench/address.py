from __future__ import annotations

import importlib.resources
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import EngineError, ErrorCode

_REQUIRED_STATE = "AZ"

_UNIT_KEYWORDS = frozenset(
    {"APT", "APARTMENT", "UNIT", "STE", "SUITE", "BLDG", "BUILDING", "FL", "FLOOR", "RM", "ROOM"}
)

_ABBREVIATIONS_RESOURCE = "usps_abbreviations.json"

_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]")
_DIGITS_RE = re.compile(r"[^0-9]")


@dataclass(frozen=True)
class NormalizedAddress:

    house_number: str
    street: str
    unit: str | None
    city: str
    state: str
    postal_code: str
    formatted_address: str
    canonical_key: str


def load_default_abbreviation_table() -> dict[str, dict[str, str]]:
    text = (
        importlib.resources.files("csr_bench")
        .joinpath(_ABBREVIATIONS_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _strip_punct(token: str) -> str:
    return _NON_ALNUM_RE.sub("", token)


def _build_lookup(abbreviation_table: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}

    def _add(raw_key: str, target: str) -> None:
        for folded in (raw_key.upper(), target.upper()):
            existing = merged.get(folded)
            if existing is not None and existing != target:
                raise EngineError(
                    ErrorCode.ADDRESS_NORMALIZATION_AMBIGUOUS,
                    f"{folded!r} maps ambiguously to both {existing!r} and {target!r}",
                )
            merged[folded] = target

    for section in abbreviation_table.values():
        if not isinstance(section, Mapping):
            continue
        for long_form, short_form in section.items():
            _add(str(long_form), str(short_form))
    return merged


def _section_lookup(section: Mapping[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    if not isinstance(section, Mapping):
        return merged
    for long_form, short_form in section.items():
        target = str(short_form)
        merged[str(long_form).upper()] = target
        merged[target.upper()] = target
    return merged


def _bad_argument(message: str) -> EngineError:
    return EngineError(ErrorCode.INVALID_ARGUMENT, message)


def _split_three(address_text: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in address_text.split(",")]
    if len(parts) != 3 or not all(parts):
        raise _bad_argument(
            "address must be exactly '<street>, <city>, <state> <zip>' "
            f"(3 comma-separated segments), got {address_text!r}"
        )
    return parts[0], parts[1], parts[2]


def _parse_street_line(raw: str) -> tuple[str, list[str], str | None]:
    tokens = raw.split()
    if not tokens:
        raise _bad_argument("address missing house number and street")
    house_number = _strip_punct(tokens[0])
    if not house_number:
        raise _bad_argument(f"house number has no digit/letter content: {tokens[0]!r}")
    rest = tokens[1:]
    if not rest:
        raise _bad_argument("address missing street name")

    unit_start: int | None = None
    for i, tok in enumerate(rest):
        if tok.startswith("#") or _strip_punct(tok).upper() in _UNIT_KEYWORDS:
            unit_start = i
            break

    if unit_start is None:
        street_tokens, unit = rest, None
    else:
        street_tokens = rest[:unit_start]
        unit_tokens = [_strip_punct(t) for t in rest[unit_start:]]
        unit = " ".join(t for t in unit_tokens if t) or None

    if not street_tokens:
        raise _bad_argument("address missing street name")
    return house_number, street_tokens, unit


def _apply_positional_abbreviations(
    tokens: list[str],
    directional_lookup: Mapping[str, str],
    suffix_lookup: Mapping[str, str],
) -> list[str]:
    cleaned = [t for t in (_strip_punct(tok) for tok in tokens) if t]
    if len(cleaned) < 2:
        return cleaned
    out = list(cleaned)
    lead = out[0].upper()
    if lead in directional_lookup:
        out[0] = directional_lookup[lead]
    tail = out[-1].upper()
    if tail in suffix_lookup:
        out[-1] = suffix_lookup[tail]
    return out


def _parse_city(raw: str) -> str:
    tokens = [_strip_punct(t) for t in raw.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise _bad_argument(f"address missing city: {raw!r}")
    return " ".join(tokens)


def _parse_state_zip(raw: str) -> tuple[str, str]:
    tokens = raw.split()
    if len(tokens) != 2:
        raise _bad_argument(f"expected '<state> <zip>', got {raw!r}")
    state_raw, zip_raw = tokens
    state = _strip_punct(state_raw).upper()
    if state != _REQUIRED_STATE:
        raise _bad_argument(f"state must be {_REQUIRED_STATE!r}, got {state_raw!r}")
    zip_digits = _DIGITS_RE.sub("", zip_raw)
    if len(zip_digits) == 5:
        postal_code = zip_digits
    elif len(zip_digits) == 9:
        postal_code = zip_digits[:5]
    else:
        raise _bad_argument(f"postal code must be 5 digits or ZIP+4, got {zip_raw!r}")
    return state, postal_code


def normalize(
    address_text: str, abbreviation_table: Mapping[str, Mapping[str, str]]
) -> NormalizedAddress:
    if not isinstance(address_text, str) or not address_text.strip():
        raise _bad_argument("address_text must be a non-empty string")

    street_raw, city_raw, state_zip_raw = _split_three(address_text)

    house_number, street_tokens, unit = _parse_street_line(street_raw)
    _build_lookup(abbreviation_table)
    directional_lookup = _section_lookup(abbreviation_table.get("directionals"))
    suffix_lookup = _section_lookup(abbreviation_table.get("suffixes"))
    abbreviated = _apply_positional_abbreviations(
        street_tokens, directional_lookup, suffix_lookup
    )
    if not abbreviated:
        raise _bad_argument("address missing street name")
    street = " ".join(abbreviated)

    city = _parse_city(city_raw)
    state, postal_code = _parse_state_zip(state_zip_raw)

    formatted_address = f"{house_number} {street}, {city}, {state} {postal_code}"
    canonical_key = formatted_address.upper()

    return NormalizedAddress(
        house_number=house_number,
        street=street,
        unit=unit,
        city=city,
        state=state,
        postal_code=postal_code,
        formatted_address=formatted_address,
        canonical_key=canonical_key,
    )


__all__ = [
    "NormalizedAddress",
    "load_default_abbreviation_table",
    "normalize",
]
