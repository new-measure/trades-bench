"""Tests for shared address normalization (spec 03 §3.3 rules 1-6; CSR-003
decision 13). This is the normalization half of 03 §15 acceptance item 5 —
the resolution half (``resolve``/``create_location``) lands with CSR-003f.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from csr_bench.address import NormalizedAddress, load_default_abbreviation_table, normalize
from csr_bench.errors import EngineError, ErrorCode

TABLE = load_default_abbreviation_table()

BASE = "1234 E Camelback Rd, Phoenix, AZ 85014"


# ---------------------------------------------------------------------------
# The pinned abbreviation table
# ---------------------------------------------------------------------------


def test_load_default_abbreviation_table_has_suffixes_and_directionals():
    table = load_default_abbreviation_table()
    assert table["suffixes"]["Road"] == "Rd"
    assert table["suffixes"]["Avenue"] == "Ave"
    assert table["suffixes"]["Boulevard"] == "Blvd"
    assert table["directionals"]["North"] == "N"
    assert table["directionals"]["Northeast"] == "NE"
    assert table["directionals"]["Southwest"] == "SW"


# ---------------------------------------------------------------------------
# Happy path shape
# ---------------------------------------------------------------------------


def test_normalize_happy_path_shape():
    r = normalize(BASE, TABLE)
    assert isinstance(r, NormalizedAddress)
    assert r.house_number == "1234"
    assert r.street == "E Camelback Rd"
    assert r.city == "Phoenix"
    assert r.state == "AZ"
    assert r.postal_code == "85014"
    assert r.unit is None
    assert r.formatted_address == BASE
    assert r.canonical_key == BASE.upper()


def test_normalize_is_idempotent_on_its_own_formatted_address():
    # 03 §3.1: canonical_key = normalize(formatted_address) — re-normalizing
    # the already-canonical string must be a no-op on both outputs.
    r = normalize(BASE, TABLE)
    r2 = normalize(r.formatted_address, TABLE)
    assert r2.canonical_key == r.canonical_key
    assert r2.formatted_address == r.formatted_address


# ---------------------------------------------------------------------------
# Rule-by-rule variants that must fold to the same canonical_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "1234 east camelback road, phoenix, az 85014",  # rule 2: case
        "  1234   E   Camelback   Rd , Phoenix ,  AZ  85014  ",  # rule 1: whitespace
        "1234 E. Camelback Rd., Phoenix, AZ 85014",  # rule 1: punctuation
        "1234 E Camelback Rd, Phoenix, AZ 85014-9999",  # rule 5: ZIP+4
    ],
)
def test_case_whitespace_punctuation_and_zip4_variants_share_canonical_key(variant):
    assert normalize(variant, TABLE).canonical_key == normalize(BASE, TABLE).canonical_key


def test_suffix_abbreviation_both_directions_share_canonical_key():
    long_form = "1234 East Camelback Road, Phoenix, AZ 85014"
    short_form = "1234 E Camelback Rd, Phoenix, AZ 85014"
    assert normalize(long_form, TABLE).canonical_key == normalize(short_form, TABLE).canonical_key


def test_directional_abbreviation_both_directions_share_canonical_key():
    long_form = "1234 Northeast Camelback Rd, Phoenix, AZ 85014"
    short_form = "1234 NE Camelback Rd, Phoenix, AZ 85014"
    assert normalize(long_form, TABLE).canonical_key == normalize(short_form, TABLE).canonical_key


# ---------------------------------------------------------------------------
# Regression: rule 3 (suffix/directional abbreviation) is positional, not
# applied to every token (review finding 1). A blind implementation corrupts
# real street names that contain a table word in an interior or otherwise
# ineligible position, which could collapse two genuinely different streets
# onto one canonical_key.
# ---------------------------------------------------------------------------


def test_leading_directional_preserves_interior_word_that_looks_directional():
    r = normalize("1234 E North Mountain Ave, Phoenix, AZ 85020", TABLE)
    assert r.street == "E North Mountain Ave"
    r2 = normalize(r.formatted_address, TABLE)
    assert r2.canonical_key == r.canonical_key


def test_leading_directional_preserves_interior_word_south():
    r = normalize("1234 W South Mountain Ave, Phoenix, AZ 85020", TABLE)
    assert r.street == "W South Mountain Ave"
    r2 = normalize(r.formatted_address, TABLE)
    assert r2.canonical_key == r.canonical_key


def test_street_named_circle_keeps_its_name_and_maps_the_real_suffix():
    r = normalize("1234 E Circle Dr, Phoenix, AZ 85020", TABLE)
    assert r.street == "E Circle Dr"
    long_form = normalize("1234 East Circle Drive, Phoenix, AZ 85020", TABLE)
    assert long_form.street == "E Circle Dr"
    assert long_form.canonical_key == r.canonical_key


def test_leading_directional_and_trailing_suffix_both_map():
    r = normalize("1234 North Central Avenue, Phoenix, AZ 85020", TABLE)
    assert r.street == "N Central Ave"


def test_single_token_street_name_never_abbreviated():
    # A street name can't consist solely of a suffix or a directional — with
    # exactly one token, neither lookup applies (protects a street literally
    # named "Circle").
    r = normalize("1234 Circle, Phoenix, AZ 85020", TABLE)
    assert r.street == "Circle"


def test_zip_plus_four_reduces_to_five_digit_zip():
    r = normalize("1234 E Camelback Rd, Phoenix, AZ 85014-6789", TABLE)
    assert r.postal_code == "85014"
    assert r.canonical_key == normalize(BASE, TABLE).canonical_key


def test_zip_plus_four_without_hyphen_also_reduces():
    r = normalize("1234 E Camelback Rd, Phoenix, AZ 850146789", TABLE)
    assert r.postal_code == "85014"


# ---------------------------------------------------------------------------
# Rule 6 — unit/apartment designator excluded from the canonical key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit_variant",
    [
        "1234 E Camelback Rd Apt 2B, Phoenix, AZ 85014",
        "1234 E Camelback Rd Unit 5, Phoenix, AZ 85014",
        "1234 E Camelback Rd Suite 300, Phoenix, AZ 85014",
        "1234 E Camelback Rd #12, Phoenix, AZ 85014",
    ],
)
def test_unit_designator_excluded_from_canonical_key_and_formatted_address(unit_variant):
    r = normalize(unit_variant, TABLE)
    base = normalize(BASE, TABLE)
    assert r.canonical_key == base.canonical_key
    assert r.formatted_address == base.formatted_address
    assert r.unit is not None


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_non_az_state_rejected():
    with pytest.raises(EngineError) as ei:
        normalize("1234 E Camelback Rd, Phoenix, CA 85014", TABLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_malformed_shape_rejected():
    with pytest.raises(EngineError) as ei:
        normalize("not a real address", TABLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_missing_house_number_rejected():
    # "!!!" strips to empty digit/letter content, so this specifically
    # exercises address.py's house-number branch (`if not house_number:
    # raise`) rather than the earlier 3-comma-segment shape check — review
    # finding 2: the original ", Phoenix, AZ 85014" input died in
    # _split_three instead, leaving the house-number branch with zero
    # coverage. Asserting on the message keeps this pinned to that branch.
    with pytest.raises(EngineError) as ei:
        normalize("!!! Camelback Rd, Phoenix, AZ 85014", TABLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    assert "house number" in ei.value.message


def test_missing_street_name_rejected():
    with pytest.raises(EngineError) as ei:
        normalize("1234, Phoenix, AZ 85014", TABLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_bad_postal_code_shape_rejected():
    with pytest.raises(EngineError) as ei:
        normalize("1234 E Camelback Rd, Phoenix, AZ 850", TABLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_empty_address_text_rejected():
    with pytest.raises(EngineError) as ei:
        normalize("   ", TABLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_ambiguous_table_rejected():
    # Two entries collide under case-folding with different targets
    # (research/osm-data.md §4.3: "a street name that maps to two different
    # abbreviations").
    bad_table = {"suffixes": {"Road": "Rd"}, "directionals": {"ROAD": "Rte"}}
    with pytest.raises(EngineError) as ei:
        normalize("1234 E Camelback Road, Phoenix, AZ 85014", bad_table)
    assert ei.value.code is ErrorCode.ADDRESS_NORMALIZATION_AMBIGUOUS


def test_ambiguous_table_detected_even_without_using_the_colliding_word():
    # The table is refused as a whole the moment it is internally
    # inconsistent — normalize() never guesses which mapping was intended.
    bad_table = {"suffixes": {"Road": "Rd"}, "directionals": {"ROAD": "Rte"}}
    with pytest.raises(EngineError) as ei:
        normalize("1234 E Camelback Ave, Phoenix, AZ 85014", bad_table)
    assert ei.value.code is ErrorCode.ADDRESS_NORMALIZATION_AMBIGUOUS


# ---------------------------------------------------------------------------
# Property test: every rule-1..5 variant of one address shares a canonical
# key, for every suffix and every directional in the shipped table.
# ---------------------------------------------------------------------------

_STREET_WORDS = (
    "Camelback",
    "Cactus",
    "Bell",
    "Thunderbird",
    "Glendale",
    # These four collide with table entries (North/South are directionals,
    # Circle/Lane are suffixes) — included so a street *named* one of these
    # words, sitting in an interior position, exercises the positional-
    # abbreviation regression the explicit tests above pin down (review
    # finding 1).
    "North",
    "South",
    "Circle",
    "Lane",
)
_SUFFIX_PAIRS = tuple(TABLE["suffixes"].items())
_DIRECTIONAL_PAIRS = ((None, None), *TABLE["directionals"].items())


def _build(
    house_number: int,
    dir_tok: str | None,
    street_word: str,
    suffix_tok: str,
    zip_tok: str,
    ws: str,
    upper: bool,
) -> str:
    parts = [str(house_number)]
    if dir_tok:
        parts.append(dir_tok)
    parts.append(street_word)
    parts.append(suffix_tok)
    text = f"{ws.join(parts)}, Phoenix, AZ {zip_tok}"
    return text.upper() if upper else text.lower()


@given(
    house_number=st.integers(min_value=1, max_value=99_999),
    street_word=st.sampled_from(_STREET_WORDS),
    suffix_pair=st.sampled_from(_SUFFIX_PAIRS),
    directional_pair=st.sampled_from(_DIRECTIONAL_PAIRS),
    zip4=st.booleans(),
    ws=st.sampled_from((" ", "  ", "   ")),
    upper=st.booleans(),
)
@settings(max_examples=200, deadline=None)
def test_property_suffix_directional_case_ws_zip4_variants_share_canonical_key(
    house_number, street_word, suffix_pair, directional_pair, zip4, ws, upper
):
    long_suffix, short_suffix = suffix_pair
    long_dir, short_dir = directional_pair
    long_zip, short_zip = "85014-6789", "85014"

    long_text = _build(
        house_number, long_dir, street_word, long_suffix, long_zip if zip4 else short_zip, ws, upper
    )
    short_text = _build(house_number, short_dir, street_word, short_suffix, short_zip, ws, upper)

    key_long = normalize(long_text, TABLE).canonical_key
    key_short = normalize(short_text, TABLE).canonical_key
    assert key_long == key_short

    if long_dir is not None:
        # street_word sits strictly between the leading directional and the
        # trailing suffix here, so it is genuinely interior. The long/short
        # equivalence above holds even under the pre-fix bug (it corrupts
        # street_word identically on both sides), so it alone cannot catch
        # review finding 1 — this asserts the interior word actually survives
        # untouched, which does.
        middle = normalize(long_text, TABLE).street.split()[1:-1]
        assert street_word.upper() in {t.upper() for t in middle}
