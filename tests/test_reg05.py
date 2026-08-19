from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _docs_engine import MASTER_SEED, docs_engine, http_call
from _helpers import geo_run_config
from trades_bench.docs_gen import build_document
from trades_bench.registry import OPERATIONS
from trades_bench.views import VIEWS
from trades_bench.wire import SPEC_VERSION

GOLDEN_DIR = Path(__file__).parent / "golden" / "reg05"
REGEN_ENV = "REGEN_GOLDEN_REG05"
_REPLAY_HEADER = "X-CSR-Idempotent-Replay"


def golden_key() -> str:
    return "__".join((SPEC_VERSION, geo_run_config().config_version, str(MASTER_SEED)))


def golden_path() -> Path:
    return GOLDEN_DIR / f"{golden_key()}.json"


def _cases() -> list[tuple[str, str, dict]]:
    document = build_document()
    examples = {
        path: item["post"]["requestBody"]["content"]["application/json"]["examples"]
        for path, item in document["paths"].items()
    }
    cases: list[tuple[str, str, dict]] = []
    for spec in OPERATIONS:
        if spec.name == "query.sql":
            continue
        path = f"/v1/ops/{spec.name}"
        cases.append((spec.name, path, examples[path]["request"]["value"]))
    sql_examples = examples["/v1/ops/query.sql"]
    for view in VIEWS:
        cases.append(
            (
                f"query.sql:{view.name}",
                "/v1/ops/query.sql",
                sql_examples[f"view.{view.name}"]["value"],
            )
        )
    return cases


def _record_in(tmp_path: Path) -> bytes:
    entries = []
    with docs_engine(tmp_path) as engine:
        for case_id, path, body in _cases():
            status, raw, headers = http_call(
                engine.port,
                "POST",
                path,
                token=engine.agent_token,
                body=body,
            )
            response = {"status": status, "body": raw.decode("ascii")}
            if _REPLAY_HEADER in headers:
                response["idempotent_replay"] = headers[_REPLAY_HEADER]
            entries.append(
                {
                    "case": case_id,
                    "request": {"method": "POST", "path": path, "body": body},
                    "response": response,
                }
            )
    document = {
        "spec_version": SPEC_VERSION,
        "config_version": geo_run_config().config_version,
        "master_seed": MASTER_SEED,
        "pairs": entries,
    }
    return (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def test_wire_goldens_replay_byte_identically(tmp_path):
    recorded = _record_in(tmp_path / "record")
    path = golden_path()
    if os.environ.get(REGEN_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recorded)
        pytest.skip(f"{REGEN_ENV}=1 rewrote {path.name}")
    assert path.is_file(), (
        f"missing wire golden {path}; rerun with {REGEN_ENV}=1 to record it deliberately"
    )
    stored = path.read_bytes()
    if stored != recorded:
        actual = tmp_path / "actual.json"
        actual.write_bytes(recorded)
        raise AssertionError(
            f"wire goldens changed for key {golden_key()}; compare {path} with {actual} "
            f"and rerun with {REGEN_ENV}=1 only for a deliberate wire change"
        )


def _stored_golden() -> dict:
    path = golden_path()
    assert path.is_file(), (
        f"missing wire golden {path}; rerun with {REGEN_ENV}=1 to record it deliberately"
    )
    return json.loads(path.read_bytes())


def test_goldens_cover_every_operation_and_every_whitelisted_view():
    stored = _stored_golden()
    cases = [entry["case"] for entry in stored["pairs"]]
    assert len(cases) == len(set(cases))
    operations = {case for case in cases if not case.startswith("query.sql:")}
    views = {case.split(":", 1)[1] for case in cases if case.startswith("query.sql:")}
    assert operations == {spec.name for spec in OPERATIONS if spec.name != "query.sql"}
    assert views == {view.name for view in VIEWS}


def test_golden_key_matches_the_recorded_run_identity():
    stored = _stored_golden()
    assert stored["spec_version"] == SPEC_VERSION
    assert stored["config_version"] == geo_run_config().config_version
    assert stored["master_seed"] == MASTER_SEED


def test_every_recorded_pair_carries_a_request_and_a_response():
    stored = _stored_golden()
    for entry in stored["pairs"]:
        assert entry["request"]["method"] == "POST"
        assert entry["request"]["path"].startswith("/v1/ops/")
        body = json.loads(entry["response"]["body"])
        assert ("data" in body) != ("error" in body)
        assert "meta" in body
