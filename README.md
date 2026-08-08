<!-- prettier-ignore -->
<div align="center">

# CSR-Bench
*A deterministic simulation foundation for home-services operations*

[Getting started](#getting-started) • [Usage](#usage) • [Architecture](#architecture)

</div>

CSR-Bench models the core contracts of a Phoenix-area HVAC, plumbing, and
electrical business. It provides replayable SQLite state, deterministic random
draws, simulated time, atomic commands, event ordering, invocation boundaries,
and an integer-cent ledger for a run spanning October 1, 2026 through
September 30, 2027.

> [!IMPORTANT]
> The repository currently implements the core contract layer, not the complete
> business simulation or a public CLI.

## Getting started

### Prerequisites

- [Python 3.13](https://www.python.org/downloads/)
- [uv](https://docs.astral.sh/uv/)

Install the project and its development dependencies:

```bash
uv sync
```

Run the test suite:

```bash
uv run pytest
```

## Usage

The repository includes a test driver that exercises the real transaction,
event, ledger, invocation, and random-number machinery:

```bash
tmpdir="$(mktemp -d)"
uv run python tests/_driver.py --db "$tmpdir/run.db" --create
```

It prints one canonical JSON report containing the logical database hash,
observed draws, stored responses, and committed script steps. Repeating the
command with the same seed and inputs produces the same logical result.

Core primitives can also be imported directly:

```python
from csr_bench.clock import Clock, phoenix_instant
from csr_bench.money import Money

start = phoenix_instant(2026, 10, 1, 6, 45)
clock = Clock(start)

print(clock.now_iso())             # 2026-10-01T06:45:00-07:00
print(Money(10_000) + Money(2_345))  # $123.45
```

> [!NOTE]
> The driver is a determinism proof harness under `tests/`, not a supported
> end-user command-line interface.

## Architecture

- `src/csr_bench/config.py` - Validated, immutable configuration and canonical hashes.
- `src/csr_bench/db.py` - SQLite schema, run identity, status, and simulated time.
- `src/csr_bench/rng.py` - Keyed draws and transaction-persisted sequential streams.
- `src/csr_bench/txn.py` - Atomic commands, privileged operations, and idempotency.
- `src/csr_bench/events.py` - Deterministically ordered event scheduling and application.
- `src/csr_bench/invocations.py` - Exclusive opening and call interaction boundaries.
- `src/csr_bench/ledger.py` - Append-only integer-cent accounting and bankruptcy checks.
- `src/csr_bench/visibility.py` - Public, privileged, hidden, and audit-only field classes.

All run state lives in one SQLite database. Private engine tables are
underscore-prefixed, and mutations are coordinated through explicit transaction
boundaries. The canonical dump helper serializes logical rows in deterministic
order so tests compare simulation state rather than SQLite file bytes.

## Configuration

The packaged defaults define the Phoenix timezone, service-day block grid,
ten service zones, and the fixed run calendar. `load_run_config()` validates
defaults and scenario overlays, rejects unknown or constant overrides, and
produces immutable canonical bytes plus a SHA-256 configuration hash.

Values awaiting later calibration are tracked separately in
`src/csr_bench/deferred_manifest.json`.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The test suite covers unit contracts, property-based money checks, fresh-process
replay, resume at every committed boundary, and resistance to read-only queries,
failed validations, and idempotent retries shifting random outcomes.
