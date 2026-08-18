#!/usr/bin/env sh
set -eu
dir="${1:-data/geography-arizona-v1}"
base="https://trades-bench-data.s3.us-east-2.amazonaws.com/map/arizona-v1"
mkdir -p "$dir/addresses" "$dir/routing"
curl -fL "$base/addresses/catalog.csv" -o "$dir/addresses/catalog.csv"
curl -fL "$base/routing/anchor_distance.bin" -o "$dir/routing/anchor_distance.bin"
uv run python - "$dir" <<'EOF'
import json
import pathlib
import sys

from trades_bench import geo_artifact

artifact_dir = pathlib.Path(sys.argv[1])
defaults = json.loads(pathlib.Path("src/trades_bench/defaults.json").read_text())["keys"]
expected = defaults["geography.artifact_sha256"]
computed = geo_artifact.canonical_artifact_sha256(artifact_dir)
if computed != expected:
    raise SystemExit(f"geography artifact sha mismatch: {computed} != {expected}")
print(f"geography artifact verified: {computed}")
EOF
