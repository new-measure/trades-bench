from __future__ import annotations

from collections.abc import Mapping

from trades_bench.address import NormalizedAddress, normalize
from trades_bench.errors import EngineError, ErrorCode

from .models import Candidate, RejectedCandidate

REASON_AMBIGUOUS = "normalization_ambiguous"

REASON_GRAMMAR_INVALID = "normalization_invalid"


def compose_address_text(house_number: str, street: str, city: str, state: str, zip_code: str) -> str:
    return f"{house_number} {street}, {city}, {state} {zip_code}"


def normalize_candidate(
    candidate: Candidate, abbreviation_table: Mapping[str, Mapping[str, str]]
) -> RejectedCandidate | None:
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
