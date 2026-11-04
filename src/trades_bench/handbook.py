from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from . import constants
from .appointments import BLOCK_CODES, OFF_HOURS_BLOCK_CODE
from .config import RunConfig
from .money import BASIS_POINTS_SCALE

HANDBOOK_FILENAME = "HANDBOOK.md"

COMPANY_NAME = "Sunridge Services"

JOB_TYPE_LABELS: Mapping[str, str] = {
    "hvac_service": "HVAC service",
    "hvac_installation": "HVAC installation",
    "plumbing": "Plumbing",
    "electrical": "Electrical",
}

DURATION_ORDER: tuple[str, ...] = ("hvac_service", "plumbing", "electrical", "hvac_installation")

_HOUR_MINUTES = 60
_HALF_HOUR_MINUTES = 30
_HOURS_FROM_MINUTES = 120


def _dollars(cents: int) -> str:
    whole, part = divmod(cents, 100)
    return f"${whole:,}" if part == 0 else f"${whole:,}.{part:02d}"


def _percent(basis_points: int) -> str:
    value = Decimal(basis_points) * 100 / BASIS_POINTS_SCALE
    return f"{value.normalize():f}%"


def _clock(minutes_from_midnight: int) -> str:
    return f"{minutes_from_midnight // 60:02d}:{minutes_from_midnight % 60:02d}"


def _window_label(start: int, end: int) -> str:
    return f"{_clock(start)}-{_clock(end)}"


def _windows() -> tuple[list[str], str]:
    labels = [_window_label(start, end) for start, end in constants.BLOCK_GRID_MINUTES]
    return labels, labels[BLOCK_CODES.index(OFF_HOURS_BLOCK_CODE)]


def _office_end() -> str:
    return _clock(constants.BLOCK_GRID_MINUTES[constants.OFFICE_BLOCK_COUNT - 1][1])


def _day_span() -> str:
    return _window_label(constants.BLOCK_GRID_MINUTES[0][0], constants.BLOCK_GRID_MINUTES[-1][1])


def _trades_section() -> list[str]:
    return [
        "## What we do",
        "",
        f"{COMPANY_NAME} serves the Phoenix area with {len(JOB_TYPE_LABELS)} kinds of work: "
        f"{', '.join(JOB_TYPE_LABELS.values())}.",
        "",
    ]


def _windows_section() -> list[str]:
    labels, _evening = _windows()
    return [
        "## Arrival windows",
        "",
        f"Customers are offered two-hour arrival windows: {', '.join(labels)}. Every job, "
        "installations included, is booked into one window. The window is the promise of "
        "when the technician arrives and starts work; typical work durations are listed below, "
        "and the work may run past the window.",
        "",
    ]


def _duration(minutes: int) -> str:
    if minutes < _HOURS_FROM_MINUTES or minutes % _HALF_HOUR_MINUTES:
        return f"{minutes} min"
    hours, rest = divmod(minutes, _HOUR_MINUTES)
    return f"{hours}½ h" if rest else f"{hours} h"


def _durations_section(config: RunConfig) -> list[str]:
    durations = config.resolved["execution.duration_minutes_by_job_type"]
    lines = ["## How long jobs take", "", "Time on site once work starts, excluding the drive:", ""]
    for code in DURATION_ORDER:
        triple = durations[code]
        lines.append(
            f"- {JOB_TYPE_LABELS[code]}: {_duration(int(triple['min']))} to "
            f"{_duration(int(triple['max']))}, typically {_duration(int(triple['mode']))}."
        )
    return [*lines, ""]


def _off_hours_section(config: RunConfig) -> list[str]:
    labels, evening = _windows()
    office = [label for label in labels if label != evening]
    evening_end = _clock(constants.BLOCK_GRID_MINUTES[-1][1])
    cap = _percent(config.get_int("payroll.overtime_cap_basis_points"))
    return [
        "## Office hours and off-hours",
        "",
        f"Office hours are the weekday windows {', '.join(office)}. The weekday evening "
        f"window ({evening}) and every weekend window are off-hours.",
        "",
        "Two technicians are on call for off-hours work: the HVAC service technician and "
        "the plumbing technician.",
        "",
        f"Every employed technician is paid {constants.WEEKDAY_PAY_MULTIPLIER} hours at "
        "their hourly wage for every weekday.",
        "On-call technicians are also paid their hourly wage for every on-call hour "
        f"({evening} on weekdays and {_day_span()} at weekends).",
        "",
        f"Overtime (capped at {cap} of the job's invoice) is paid at 1x the technician's "
        "hourly wage for work and driving outside the hours they are already paid for: "
        f"after {_office_end()} on a weekday, or after {evening_end} for an on-call "
        "technician.",
        "",
    ]


def _money_section(config: RunConfig) -> list[str]:
    return [
        "## Running costs and cash",
        "",
        f"- Operating overhead: {_dollars(config.get_int('finance.daily_operating_overhead'))} "
        "per day.",
        f"- Starting cash: {_dollars(config.get_int('finance.starting_cash'))}.",
        "",
    ]


def render_handbook(config: RunConfig) -> str:
    lines = [f"# {COMPANY_NAME} company handbook", ""]
    lines += _trades_section()
    lines += _windows_section()
    lines += _durations_section(config)
    lines += _off_hours_section(config)
    lines += _money_section(config)
    return "\n".join(lines).rstrip("\n") + "\n"


def write_handbook(target_dir: str | Path, config: RunConfig) -> Path:
    directory = Path(target_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / HANDBOOK_FILENAME
    path.write_bytes(render_handbook(config).encode("utf-8"))
    return path


__all__ = [
    "HANDBOOK_FILENAME",
    "render_handbook",
    "write_handbook",
]
