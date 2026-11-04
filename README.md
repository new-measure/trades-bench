<div align="center">

# Trades Bench

*How good are LLMs at running a simulated home services business?*

[Blog](https://newmeasure.ai/benchmarks/tradesbench) · [Explore the published runs](https://huggingface.co/datasets/adityabressl/trades-bench)

</div>


Trades Bench measures how well an AI agent can operate Sunridge Services, a fictional
home services company providing HVAC, plumbing, and electrical services. During two simulated
weeks, the agent handles customer calls, books jobs and dispatches four
technicians. The simulation considers real routes across the Phoenix area road network and weather conditions to estimate drive time of the technician to the customer. The benchmark reports profit, defined as final cash minus the $25,000 starting cash after the 14 day run.

Good service grows the business: every job done on time brings a little more demand the next
day, while a missed or failed visit loses customers and cuts demand.
Weather, cancellations, variable job lengths, and drive times decide the rest.

The benchmark is designed for exact replay. The run seed is hashed with the
identity of each uncertain event, so identical seeds and actions produce identical
simulated world. This remains true when a run resumes from a copied SQLite file.

## Results

The [full write-up](https://newmeasure.ai/benchmarks/tradesbench) explains the
methodology and reports the evaluation: five models on seed 5. We ran each model
multiple times, and the table shows the best run of each model. Profit is final
cash minus the $25,000 starting cash.

| Run                                    | Profit             |
| -------------------------------------- | ------------------ |
| Oracle (scripted reference policy)     | +$30,289.56        |
| GPT-6 Astra, xhigh thinking effort     | +$23,934.51        |
| Gemini 4 Argon, high thinking effort   | +$23,431.93        |
| Claude Opus 5.5, xhigh thinking effort | +$23,118.20        |
| GPT-6.1 Sol, xhigh thinking effort     | +$21,911.92        |
| Muse Wonder, xhigh thinking effort     | +$11,432.48        |
| Reject-heavy baseline                  | Bankrupt on day 11 |
| Do-nothing baseline                    | Bankrupt on day 10 |
| Accept-all baseline                    | Bankrupt on day 12 |

The [published dataset](https://huggingface.co/datasets/adityabressl/trades-bench)
contains each model's best run on seed 5: the policy report, the full run database
(`run.db`), complete per-turn agent logs, and the agent's final workspace. It also
has the reference policy reports and the geography artifact used to build the
Phoenix area road network and address catalog.

## What's in the repository

- `[src/trades_bench/](src/trades_bench/)` contains the simulation engine.
- `[tools/policies/](tools/policies/)` contains the benchmark driver, the LLM agent
runner, and scripted baseline policies.
- `[tools/geo_build/](tools/geo_build/)` builds the geography artifact from an
OpenStreetMap extract. It requires Docker and the `geo` dependency group.
- `[data/geography-arizona-v1/](data/geography-arizona-v1/)` contains the map
artifact. Map data is from OpenStreetMap using a Geofabrik
extract. The artifact used in this benchmark is [published on Hugging Face](https://huggingface.co/datasets/adityabressl/trades-bench/tree/main/geography-arizona-v1).



## Quick start

Install [uv](https://docs.astral.sh/uv/) and Python 3.13 or newer, then sync the
environment and run the full test suite:

```bash
uv sync
uv run pytest -q
```

To rebuild and verify the geography artifact, start Docker and run:

```bash
uv sync --group geo
uv run --group geo pytest -m docker -q
```

## Run the benchmark

Fetch and verify the geography artifact, which the benchmark image bundles:

```bash
sh tools/fetch_geography.sh
```

Then build the image and run a model with [Docker](https://docs.docker.com/get-docker/)
on macOS, Linux, or Windows. This is the same isolation used for the published runs, except
for the network: a local container has open internet access, while the published runs could
reach only the model provider's API and `pi.dev`.

```bash
docker build -t trades-bench .
mkdir -p runs
docker run --rm \
  -e OPENAI_API_KEY \
  -e TB_PROVIDER=openai \
  -e TB_MODEL=gpt-5.6-luna \
  -e TB_THINKING=high \
  -v "$PWD/runs:/var/lib/trades-bench/results" \
  trades-bench
```

`TB_PROVIDER`, `TB_MODEL`, and `TB_THINKING` are required. The provider is one of
`openai`, `anthropic`, `google`, `xai`, or `meta`, and needs its Pi API key variable:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, or
`META_API_KEY`. Optional settings are `TB_SEED` (default `5`), `TB_SCENARIO` (default
`oct_default`), and `TB_LABEL` (default `<model>-<thinking>-a1`). Results are written to
`runs/<label>/`, including the policy report, the run database, per-turn logs, and the
agent's final workspace. The container refuses a label that already holds results, so
set `TB_LABEL` (for example `-e TB_LABEL=gpt-5.6-luna-high-a2`) for each further attempt.

To
stop a run early, use `docker stop -t 120 <container>`. The container then records the run
as failed with reason `interrupted`, writes the usage so far, and copies the run database
before it exits.

### Development runs

You can also run the driver directly on your machine. This path is for development
only and is not isolated. Install [Node.js](https://nodejs.org/) and
[pnpm](https://pnpm.io/installation), then install Pi and authenticate it with your
provider:

```bash
pnpm add -g --ignore-scripts @earendil-works/pi-coding-agent
pi
```

Run `/login` in Pi to select a subscription provider, or configure an API key. For
example, run this seed-5 experiment:

```bash
PYTHONPATH=tools uv run python -m policies.pi_run \
  --run-dir runs/seed-5 \
  --seed 5 \
  --provider openai-codex \
  --model gpt-5.6-luna \
  --thinking high
```

`--provider`, `--model`, and `--thinking` are required. Use `--executable` to select
a Pi executable other than `pi`.
