"""Tests for the user-decided constants registry (00-decisions + decision 3)."""

from __future__ import annotations

from csr_bench import clock
from csr_bench import constants as const_mod


def test_ten_phoenix_zones():
    names = const_mod.ZONE_NAMES
    assert len(names) == 10
    assert names[0] == "Central Phoenix"
    assert names[-1] == "West Valley/Goodyear"
    assert len(set(names)) == 10  # unique


def test_opening_and_call_window_and_blocks_are_decision_values():
    assert const_mod.OPENING_TIME == (6, 45)
    assert const_mod.CALL_WINDOW == (420, 1140)  # 07:00, 19:00 (min)
    assert const_mod.OFFICE_BLOCK_COUNT == 5
    grid = const_mod.BLOCK_GRID_MINUTES
    assert len(grid) == 6  # five office + after-hours
    # contiguous, six 120-minute blocks spanning 07:00..19:00
    assert grid[0] == (420, 540)
    assert grid[-1] == (1020, 1140)
    for prev, cur in zip(grid, grid[1:], strict=False):
        assert prev[1] == cur[0]
        assert cur[1] - cur[0] == 120


def test_timing_variation_triangular_15_0_15():
    assert const_mod.TIMING_VARIATION == (-15, 0, 15)
    assert const_mod.GRACE_MINUTES == 10


def test_install_and_workforce_constants():
    assert const_mod.INSTALL_BLOCKS == 2
    assert const_mod.INSTALL_MAX_START_MIN == 13 * 60  # never after 13:00
    assert const_mod.STARTING_TECHNICIANS == 4
    assert const_mod.SICKNESS_DAYS == (1, 3)
    assert const_mod.WEEKDAY_PAY_MULTIPLIER == 10
    assert const_mod.UNANSWERED_ATTEMPTS == 3


def test_calendars():
    assert const_mod.WARM_START_RANGE == ("2026-04-01", "2026-09-30")
    assert const_mod.RUN_CALENDAR == ("2026-10-01", "2027-09-30")
    assert const_mod.PHOENIX_TIMEZONE == "America/Phoenix"


def test_constant_entries_all_marked_constant_in_config():
    from csr_bench.config import assemble_registry

    reg = assemble_registry()
    for path, spec in reg.items():
        if path.startswith("constants."):
            assert spec.constant is True, f"{path} should be a constant"
        else:
            # domain keys (finance.*, hiring.*) are scenario knobs, not constants
            assert spec.constant is False, f"{path} should not be a constant"


def test_clock_sources_block_grid_from_constants_single_source():
    # The clock must not redefine the grid; it reads it from constants.
    grid = const_mod.BLOCK_GRID_MINUTES
    assert clock.OFFICE_BLOCKS == grid[: const_mod.AFTER_HOURS_BLOCK_INDEX]
    assert clock.AFTER_HOURS == grid[const_mod.AFTER_HOURS_BLOCK_INDEX]
    assert const_mod.AFTER_HOURS_BLOCK_INDEX == 5
    assert len(clock.OFFICE_BLOCKS) == 5


def test_constant_values_lookup_opening_as_validated_value():
    assert const_mod.CONSTANT_VALUES["constants.opening_hour"] == 6
    assert const_mod.CONSTANT_VALUES["constants.zone_names"] == list(const_mod.ZONE_NAMES)
