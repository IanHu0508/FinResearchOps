# FinResearchOps

Financial research engineering built around two deliberately separate pieces:

1. a TradingAgents-based investment-research workflow whose information flow and financial calculations are constrained for review; and
2. an experimental A-share quantitative-research module for causal market features, forward labels and walk-forward evaluation.

The Quant module does **not** feed the Agent workflow, and neither component is presented as a trading system or as evidence of investment performance.

## What changes relative to the TradingAgents baseline

The upstream graph, role topology and native tools are retained. This project changes what roles see, how they revise claims, and how final financial numbers are produced:

- bull and bear researchers write independent first drafts from the same source material;
- each side then responds to the other side's sealed draft and records what it maintains, changes or leaves unresolved;
- the final assessment does not inherit earlier ratings or trader-defined thresholds;
- forward assumptions are stored explicitly and recalculated in Python before the final report is written;
- report figures reference the effective recalculated outputs rather than free-form numbers copied by the model;
- model traces, failed outputs and saved cases are retained so completed steps can be reopened or replayed.

These controls narrow specific failure modes; they do not prove that the model's research judgment is correct.

## One concrete offline example

The public synthetic demo exercises a minority-interest correction through the current research path:

- the original scenario produces EPS of **1.7**;
- correcting minority-profit attribution changes EPS to **1.3**;
- consolidated operating cash flow remains **16**, because that correction does not change the consolidated cash-flow calculation;
- an intentionally unbound forecast number is rejected instead of being saved into the report.

Run it without an API key or live model:

```bash
mkdir -p ../private
python3.12 -m venv ../tmp/tradingagents-runtime
../tmp/tradingagents-runtime/bin/python -m pip install -r requirements/tradingagents.lock
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python scripts/thesis_offline_demo.py \
  --artifact-root ../private/thesis-demo
```

This is a scripted engineering demonstration, not a model-quality or investment-performance evaluation.

## Separate A-share Quant module

The independent [Quant module](quant/README.md) defines 60-session market inputs, 20-session forward-return targets, purged date splits, stock-only/context ablations, Rank IC evaluation and versioned research signals.

The first real-data panel processed more than four million code-day records, but its research results were **invalidated** after old and new ticker aliases for the same securities were counted separately. That error changes universe membership, market context, labels and evaluation weights, so the affected IC results are retained only for audit rather than presented as performance.

See [current status](docs/status.md) for the exact evidence, limitations and next repair.

## TradingAgents research integration

Use [`research-thesis`](docs/thesis-research.md) for the main workflow. It keeps
the native analyst/research/trader/risk/portfolio topology and routing while
changing role prompts and information flow. Complete native tool returns, or a
frozen source bundle, reach the research roles without the old field projection.
Models can give an actual rating or `REVIEW` when there is no defensible rating.

The original `tradingagents-baseline` remains a separate comparison route.
The [filing-focused component](docs/tradingagents-research.md) and
[restricted native audit route](docs/native-audit.md) retain their independent
and historical uses; they are not steps inside `research-thesis`. In particular,
the old typed route's compatibility Hold must not be treated as an investment
rating. All model-backed routes use the separate integration environment.

## Try the current research workflow offline

This example follows `research-thesis`, including a scripted parameter correction,
program recomputation, final-report number references, saving and reopening.
It uses synthetic model responses and blocks external network paths; it is a
mechanism demonstration, not a model-quality or investment-performance result.

From a checkout named `finaudit-gate`, prepare the separate integration environment:

```bash
mkdir -p ../private
python3.12 -m venv ../tmp/tradingagents-runtime
../tmp/tradingagents-runtime/bin/python -m pip install -r requirements/tradingagents.lock
PYTHONPATH=src ../tmp/tradingagents-runtime/bin/python scripts/thesis_offline_demo.py \
  --artifact-root ../private/thesis-demo
```

Dependency installation uses the network. Running the example needs no API key,
model download or issuer filing. It shows a minority-profit attribution correction:
EPS changes from 1.7 to 1.3 while consolidated operating cash flow remains 16.
Add `--bad-prose` and use a new output directory to demonstrate rejection of an
unbound forecast number. Original inputs and failed responses remain available.

The [research workflow guide](docs/thesis-research.md) describes the request,
correction and report contracts. [Offline CI](.github/workflows/offline.yml)
covers the core, isolated wheel installation, native integration and these demos;
its execution status is recorded in [project status](docs/status.md).

The following cash-flow and profile examples are retained component workflows.

## Run a cash-flow investigation

```bash
PYTHONPATH=src .venv/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-output-directory> \
  investigate-cashflow \
  --source-manifest <absolute-acquisition-provenance-json> \
  --comparison-end 2024-12-31 --cutoff 2026-09-05 \
  --currency CNY --strategy rules
```

Select `--strategy adaptive` to use the already installed local 8B model.
The output includes a workpaper path, Case reference and offline-replay run ID.
No software, filings or model weights are downloaded by the command.

The supported format, missing-value treatment, source manifest, small module
map and draft-only review boundary are documented in
[`docs/cashflow-investigation.md`](docs/cashflow-investigation.md).

## The two layers and the retained profile-based task

- **FinAuditGate** is the core. Given one frozen document and one question, it
  can run either the source-only cash-flow task or the retained reviewed-profile
  task. The latter takes a candidate from a model (two evidence spans, their financial
  semantics, one allowlisted formula), checks every claim against the frozen
  bytes and a reviewed profile, computes the answer with `Decimal`, and returns
  exactly one of `ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW`. Every run is a set
  of append-once, content-addressed artifacts that a second gate with no model
  can replay.
- **FinResearchOps** is the workflow around it: a Case is created from a frozen
  filing, analysed once, inspected as a Workpaper, reviewed by a person
  (`APPROVE / RETURN / REJECT`), and only then exported as a proposal-only
  Research Change Packet.

```text
acquired annual filing
   → source facts + financial reconciliation
   → choose a driver → search notes → return evidence or missing-input feedback
   → local draft workpaper for human review
   → offline replay of source calculations and recorded actions
```

Current status lives in one place: [`docs/status.md`](docs/status.md).

## Quick start

The core package is standard-library only and requires Python 3.12. From a
fresh checkout, create the local environment before running the offline tests:

```bash
git clone https://github.com/IanHu0508/FinResearchOps.git finaudit-gate
cd finaudit-gate
python3.12 -m venv .venv
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

The synthetic demo below needs no model, API key or issuer filing. Cloud-model
setup is separate; see the [TradingAgents integration guide](docs/tradingagents-research.md).
Keep the checkout directory named `finaudit-gate`: private-run path checks
expect a sibling `private/` directory next to that Git worktree.

Run the public synthetic profile end to end and replay it without a model:

```bash
PYTHONPATH=src .venv/bin/python scripts/synthetic_demo.py --artifact-root .local/demo
```

Build and verify the installed package in a disposable environment:

```bash
W="$(mktemp -d)"
$HOME/.local/bin/uv build --wheel --out-dir "$W/dist" --offline
$HOME/.local/bin/uv venv --python 3.12.13 --managed-python "$W/venv"
$HOME/.local/bin/uv pip install --python "$W/venv/bin/python" --offline "$W"/dist/*.whl
env -u PYTHONPATH "$W/venv/bin/finresearchops" --help
```

## Running the retained reviewed-profile task

`finresearchops` is a thin CLI over the Application Interface
(`handle(command)` / `read_case(case_ref)`). It needs a local Ollama daemon
with the frozen tag installed, and every private input must live under the
workspace's sibling `private/` tree:

```bash
finresearchops --artifact-root /…/private/runs/dev/example \
  create-case --mode PRIVATE_DEV --document /…/private/…/slice.txt \
  --source-id tencent-2025-annual-report --published-at 2026-04-09 \
  --cutoff 2026-05-01 --question "…"
finresearchops --artifact-root /…/private/runs/dev/example \
  run-analysis --case-ref case-… --model-trace-root /…/private/model-traces \
  --validation-profile /…/private/profiles/case-01.json
```

The complete procedure, including how to build the validation profile from
reviewed facts, is in [`docs/runbook-private-case.md`](docs/runbook-private-case.md).
The CLI actions are documented in [`docs/cli.md`](docs/cli.md).

## Verification and claims

The offline suite covers financial rules, the two task paths, private storage,
model contracts, Application persistence and replay. Test results are not
financial evaluation results. See [`docs/status.md`](docs/status.md) for dated
observations and [`docs/evaluation-protocol.md`](docs/evaluation-protocol.md)
for the evaluation rules.

## Repository layout

- `src/finauditgate/` — core (`core/`), Application (`application/`), model
  Adapters (`adapters/`), the paired-evaluation helper (`evaluation/`), CLI.
- `tests/` — offline `unittest` suite.
- `fixtures/synthetic/` — original synthetic text and inline-XBRL fixtures.
- `schemas/` — JSON Schemas for every persisted artifact, one version each.
- `manifests/examples/` — public-safe route and source metadata.
- `scripts/` — the synthetic demo and the validation-profile builder.
- `docs/` — architecture, status, CLI, runbook, data policy, evaluation protocol.

## Data

The repository contains only original code, synthetic fixtures, schemas, and
public-safe metadata. Issuer documents, extracted text, validation profiles,
raw model traces, and manual QA stay in the sibling `private/` directory and
never enter Git. See [`docs/data-policy.md`](docs/data-policy.md) and
[`NOTICE_DATA.md`](NOTICE_DATA.md).

## License

Original code, documentation, schemas, and synthetic fixtures are licensed
under the [Apache License 2.0](LICENSE). Third-party filings, issuer names and
marks, model weights, and evaluation material are not relicensed.
