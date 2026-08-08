"""CSR-Bench — deterministic home-services business simulation engine.

Coordinate contracts shared by every engine module live here. This package owns
no domain formulas; it provides the run identity, clock, money, configuration,
deterministic randomness, transaction boundary, event envelope, invocation
exclusivity, money/bankruptcy mechanics, and the field-visibility
classification registry (``classify`` / ``assert_public`` in
:mod:`csr_bench.visibility`).

See ``specs/01-run-contracts.md`` for the normative contract and
``docs/CSR-001.md`` for the binding ticket decision log.
"""

from __future__ import annotations

#: The spec version this engine implements. Adding a member to any closed enum
#: or a named RNG stream is a spec-version change (spec 01 §Canonical primitive
#: types, §Deterministic randomness).
SPEC_VERSION: str = "1.0.0"

__all__ = ["SPEC_VERSION"]
