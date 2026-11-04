# Run viewer

Replays a Trades Bench run in the browser with one shared timeline and two 3D views:

- **Office**: the dispatcher at the desk. Calls come in on the phone and the monitor, the agent's
  reasoning and tool calls stream in, a stamp shows the decision (booked / rejected / lost /
  cancellation), the wall whiteboard shows the day's schedule, and the notebook panel shows
  `MEMORY.md` changing as the agent learns (new lines highlighted, removed ones crossed out).
- **Field**: the Phoenix service area. Technicians drive between home and jobs, job pillars glow
  while work is underway and grow with revenue once paid, rejected/lost calls appear as markers, and
  payroll, overhead and on-call charges post at close. Click a pillar for the job's full story.

## Data sources

`build/sources.py` uses whatever a run folder has and records where each piece came from (see
**Sources** in the viewer):

| Piece | Preferred source | Fallback |
| --- | --- | --- |
| Calls, reasoning, tool calls | `pi-logs/` | — |
| Field (jobs, drives, money) | `run.db` (recorded) | `rebuilt/run.db` from `rebuild_run.py` |
| Notebook history | `rebuilt/memory-history.jsonl` | edit/write tool calls only (approximate) |
| Cash curve | `run.db` ledger | `policy-report.json` daily cash |

Published Hugging Face runs include `run.db`, so they need no rebuild. For a run without one,
`build/rebuild_run.py` recreates it: it drives the real engine through the real harness and answers
each prompt by re-running that turn's recorded tool calls in a Docker sandbox laid out like the
original agent host. The result is checked against the `dump_hash` in `policy-report.json`, so a
verified rebuild is the exact world the agent played.

## Usage

Requires Docker (for rebuilds), uv, and Node 20+.

```bash
# 1. fetch a run (any path under runs/ on the dataset)
python3 viz/build/fetch_run.py seed5/claude-opus-5-5-xhigh-a1

# 2. only if the run has no run.db: rebuild it (~10 min), verified against dump_hash
uv run python viz/build/rebuild_run.py runs/seed5/claude-opus-5-5-xhigh-a1

# 3. build the viewer data (any number of runs)
uv run python viz/build/build_replay.py runs/seed5/claude-opus-5-5-xhigh-a1

# 4. open the viewer
cd viz/app && npm install && npm run dev
```

Keys: `space` play/pause, `←`/`→` previous/next call, `1`/`2` switch tabs. The timeline freezes
office time while the agent works a call, and runs simulated time fast between calls.

Model logos in `app/public/logos/` are the official marks as packaged by
[LobeHub Icons](https://github.com/lobehub/lobe-icons) (`@lobehub/icons-static-svg`, MIT). The marks remain
trademarks of their owners and identify which model ran.
