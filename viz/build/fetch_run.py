from __future__ import annotations

# Download a run (or part of one) from the published Hugging Face dataset into runs/.
#   python3 viz/build/fetch_run.py seed5/opus5-xhigh-a1
#   python3 viz/build/fetch_run.py seed5/opus5-xhigh-a1 --include 'policy-report.json'
# Whatever artifacts the dataset has for the run (run.db, index.jsonl, workspace/, ...) are
# fetched too, so a richer upload is picked up without code changes.

import argparse
import fnmatch
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATASET = "adityabressl/trades-bench"
API = f"https://huggingface.co/api/datasets/{DATASET}/tree/main/"
RESOLVE = f"https://huggingface.co/datasets/{DATASET}/resolve/main/"
REPO = Path(__file__).resolve().parents[2]


def list_files(prefix: str) -> list[dict]:
    url = API + urllib.parse.quote(prefix) + "?recursive=true&limit=1000"
    files: list[dict] = []
    while url:
        with urllib.request.urlopen(url, timeout=60) as response:
            files += json.load(response)
            link = response.headers.get("link") or ""
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else ""
    return [f for f in files if f["type"] == "file"]


def download(entry: dict, dest_root: Path) -> bool:
    out = dest_root / entry["path"]
    if out.exists() and out.stat().st_size == entry["size"]:
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            urllib.request.urlretrieve(RESOLVE + urllib.parse.quote(entry["path"]), out)
            if out.stat().st_size == entry["size"]:
                return True
        except OSError:
            pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser(prog="fetch_run.py")
    parser.add_argument("run", help="path under runs/ on the dataset, e.g. seed5/opus5-xhigh-a1")
    parser.add_argument("--include", action="append", default=[], help="glob(s) relative to the run folder")
    parser.add_argument("--dest", type=Path, default=REPO)
    args = parser.parse_args()

    prefix = f"runs/{args.run.strip('/')}"
    files = list_files(prefix)
    if args.include:
        files = [f for f in files if any(fnmatch.fnmatch(f["path"][len(prefix) + 1 :], g) for g in args.include)]
    if not files:
        raise SystemExit(f"nothing found under {prefix}")
    total = sum(f["size"] for f in files)
    print(f"{len(files)} files, {total / 1e6:.1f} MB -> {args.dest / prefix}")
    with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as pool:
        results = list(pool.map(lambda f: download(f, args.dest), files))
    failed = [f["path"] for f, ok in zip(files, results) if not ok]
    for path in failed:
        print("FAILED", path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
