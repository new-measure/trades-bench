"""Shared address normalization (spec 03 §3.3; CSR-003 decision 13).

One :func:`normalize` serves the build pipeline (``tools/geo_build``,
CSR-003b+), ``create_location``, and ``maps.distance`` (CSR-003e/f) so
build-time and run-time normalization can never drift from each other. It is
pure and stdlib-only, and it takes the abbreviation table as a parameter
rather than importing a fixed copy: at build time the caller loads this
package's pinned :func:`load_default_abbreviation_table`; at run time it loads
the frozen geography artifact's own packaged copy instead
(``research/osm-data.md`` §6) — same bytes, never a re-derivation.

Address text is expected in the canonical three-segment layout the catalog's
own ``formatted_address`` uses and that a call prompt copies verbatim
(03 §3.1, §3.3)::

    "<house number> <street> [<unit>], <city>, <state> <zip>[-<+4>]"

Any other shape is rejected with ``ErrorCode.INVALID_ARGUMENT`` — normalize()
never guesses at a different address grammar (03 §3.3: "The engine Must not
perform fuzzy or nearest-neighbour matching").
"""

from __future__ import annotations

import importlib.resources
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import EngineError, ErrorCode

#: The only state v1 serves (03 §3.3 rule 4) — a structural literal from the
#: spec text itself, not a calibration value.
_REQUIRED_STATE = "AZ"

#: Recognized unit/apartment designator keywords (03 §3.3 rule 6): a street
#: token matching one of these, case-insensitively, starts the unit segment
#: excluded from the canonical key. Closed vocabulary, not a calibration
#: number — akin to the enum members in :mod:`csr_bench.enums`.
_UNIT_KEYWORDS = frozenset(
    {"APT", "APARTMENT", "UNIT", "STE", "SUITE", "BLDG", "BUILDING", "FL", "FLOOR", "RM", "ROOM"}
)

#: Packaged copy of the pinned USPS-style table (decision 13); the pipeline
#: ships an identical copy inside the artifact (research/osm-data.md §6).
_ABBREVIATIONS_RESOURCE = "usps_abbreviations.json"

_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]")
_DIGITS_RE = re.compile(r"[^0-9]")


@dataclass(frozen=True)
class NormalizedAddress:
    """The result of :func:`normalize` (03 §3.1 catalog columns, §3.3 rules).

    ``unit`` is carried separately from every other field precisely because
    rule 6 excludes it from both ``formatted_address`` and ``canonical_key`` —
    it is not part of the catalog row (03 §3.1: "There is no `unit` column").
    """

    house_number: str
    street: str
    unit: str | None
    city: str
    state: str
    postal_code: str
    formatted_address: str
    canonical_key: str


def load_default_abbreviation_table() -> dict[str, dict[str, str]]:
    """The repo's pinned USPS-style abbreviation table.

    Tests and the shipped defaults load this packaged copy directly; at run
    time the table comes from the frozen geography artifact instead (decision
    13), so this function is never called from a scored run.
    """
    text = (
        importlib.resources.files("csr_bench")
        .joinpath(_ABBREVIATIONS_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _strip_punct(token: str) -> str:
    return _NON_ALNUM_RE.sub("", token)


def _build_lookup(abbreviation_table: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """Validate the *whole* table's internal consistency and return the merged
    case-insensitive lookup (kept for callers that want it, e.g. tests).

    Raises ``ADDRESS_NORMALIZATION_AMBIGUOUS`` the moment two entries collide
    under case-folding with different targets — a street name (or a target
    abbreviation) that would map to two different canonical forms
    (research/osm-data.md §4.3). Checked across every section combined
    (suffixes, directionals, ...), independent of the address text and of
    which section :func:`_section_lookup` later applies at which token
    position — the table must be self-consistent even though rule 3 only ever
    consults one section at a time.
    """
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
    """Case-insensitive token -> abbreviation for exactly *one* table section
    (only directionals, or only suffixes), plus an identity entry for the
    already-abbreviated form, so "Rd" and "Road" fold to the same target.

    Callers must run :func:`_build_lookup` over the whole table first — this
    does not repeat the ambiguity check, since positional application (see
    :func:`_apply_positional_abbreviations`) never consults two sections for
    the same token, and the whole-table check already proved consistency.
    """
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
    """Rule 3 maps "street suffixes and directional prefixes" (03 §3.3 rule
    3, spec text at 03:158) — positional roles, not arbitrary tokens. Mapping
    every token blindly corrupts real street names that happen to contain a
    table word in an interior position — "North Mountain Ave" would lose
    "North" and collapse onto a different street's canonical key, and a
    street literally named "Circle" would lose its identity to the "Cir"
    suffix abbreviation.

    - Leading token: directional lookup only, and only with >= 2 tokens (a
      street cannot consist solely of a directional).
    - Trailing token: suffix lookup only, and only with >= 2 tokens (a street
      cannot consist solely of a suffix — protects a street named "Circle").
    - Every other token, including the sole token of a single-word street,
      passes through with punctuation stripped but never abbreviation-mapped.
    """
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
        postal_code = zip_digits[:5]  # ZIP+4 -> 5-digit ZIP (rule 5)
    else:
        raise _bad_argument(f"postal code must be 5 digits or ZIP+4, got {zip_raw!r}")
    return state, postal_code


def normalize(
    address_text: str, abbreviation_table: Mapping[str, Mapping[str, str]]
) -> NormalizedAddress:
    """Apply spec 03 §3.3 rules 1-6 and return the canonical form.

    Rule 3 (street suffixes and directional prefixes) is applied
    positionally, not to every token — see
    :func:`_apply_positional_abbreviations`. Idempotent by construction:
    calling ``normalize`` again on the returned ``formatted_address`` yields
    the identical ``canonical_key`` — the property 03 §3.1 relies on when it
    defines ``canonical_key = normalize(formatted_address)``.
    """
    if not isinstance(address_text, str) or not address_text.strip():
        raise _bad_argument("address_text must be a non-empty string")

    street_raw, city_raw, state_zip_raw = _split_three(address_text)  # rule 1 (structure)

    house_number, street_tokens, unit = _parse_street_line(street_raw)  # rules 1, 6
    _build_lookup(abbreviation_table)  # whole-table consistency; raises if ambiguous
    directional_lookup = _section_lookup(abbreviation_table.get("directionals"))
    suffix_lookup = _section_lookup(abbreviation_table.get("suffixes"))
    abbreviated = _apply_positional_abbreviations(  # rules 2, 3
        street_tokens, directional_lookup, suffix_lookup
    )
    if not abbreviated:
        raise _bad_argument("address missing street name")
    street = " ".join(abbreviated)

    city = _parse_city(city_raw)  # rule 1
    state, postal_code = _parse_state_zip(state_zip_raw)  # rules 4, 5

    formatted_address = f"{house_number} {street}, {city}, {state} {postal_code}"
    canonical_key = formatted_address.upper()  # rule 2: case-insensitive match key

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
