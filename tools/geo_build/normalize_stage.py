"""Normalize stage (research/osm-data.md §4.3).

Composes the exact three-segment grammar :func:`csr_bench.address.normalize`
requires — ``"<house#> <street> [<unit>], <city>, <state> <zip>"`` — from a
candidate's extracted fields, then calls the *shared* function verbatim.
This module never reimplements any normalization rule; it only builds the
input string and translates ``normalize()``'s two failure shapes into the
pipeline's rejection reasons.
"""

from __future__ import annotations

from collections.abc import Mapping

from csr_bench.address import NormalizedAddress, normalize
from csr_bench.errors import EngineError, ErrorCode

from .models import Candidate, RejectedCandidate

#: research/osm-data.md §4.3: "A candidate whose normalization is ambiguous
#: ... is rejected with reason normalization_ambiguous."
REASON_AMBIGUOUS = "normalization_ambiguous"

#: Not named by the spec text, which only calls out the ambiguous-table case;
#: `normalize()` can also reject on ordinary grammar/shape grounds (an empty
#: street after punctuation stripping, a house number with no digit content,
#: ...) that have nothing to do with table ambiguity. The ticket's own
#: instruction ("grammar-level failures also rejected with a recorded
#: reason") names this second bucket without pinning its string; this is the
#: pipeline's choice, recorded here and in `docs/tickets/CSR-003.md`.
REASON_GRAMMAR_INVALID = "normalization_invalid"


def compose_address_text(house_number: str, street: str, city: str, state: str, zip_code: str) -> str:
    """Exactly the layout `normalize()` requires (03 §3.1's own
    `formatted_address` shape, before abbreviation is applied)."""
    return f"{house_number} {street}, {city}, {state} {zip_code}"


def normalize_candidate(
    candidate: Candidate, abbreviation_table: Mapping[str, Mapping[str, str]]
) -> RejectedCandidate | None:
    """Mutate ``candidate`` in place with the normalized fields on success;
    return a :class:`RejectedCandidate` (candidate untouched otherwise) on
    failure.
    """
    address_text = compose_address_text(
        candidate.house_number_raw,
        candidate.street_raw,
        candidate.city,
        candidate.state,
        candidate.postal_code_raw,
    )
    try:
        result: NormalizedAddress = normalize(address_text, abbreviation_table)
    except EngineError as exc:
        if exc.code is ErrorCode.ADDRESS_NORMALIZATION_AMBIGUOUS:
            reason = REASON_AMBIGUOUS
        elif exc.code is ErrorCode.INVALID_ARGUMENT:
            reason = REASON_GRAMMAR_INVALID
        else:  # pragma: no cover - normalize() raises only these two codes
            raise
        return RejectedCandidate(
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            reason=reason,
            detail=str(exc),
        )

    candidate.house_number = result.house_number
    candidate.street = result.street
    candidate.unit = result.unit
    candidate.city = result.city
    candidate.state = result.state
    candidate.postal_code = result.postal_code
    candidate.formatted_address = result.formatted_address
    candidate.canonical_key = result.canonical_key
    return None


__all__ = [
    "REASON_AMBIGUOUS",
    "REASON_GRAMMAR_INVALID",
    "compose_address_text",
    "normalize_candidate",
]
