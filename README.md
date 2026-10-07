# FinResearchOps

[Public project showcase](https://ianhu0508.github.io/FinResearchOps/) · [Research and verification status](docs/status.md)

**Financial research from independent views to recomputed forecasts, scenario valuation and evidence-linked reports, built on TradingAgents and an A-share quantitative research pipeline.**

FinResearchOps connects company evidence, operating assumptions and investment judgments in one recorded workflow. Models develop hypotheses and revise them against counterevidence. Python computes earnings, cash and conditional prices, and the report cites the effective results.

TradingAgents supplies the native role graph. My contributions are the judgment-update protocol, financial recomputation and correction loop, research delivery and recovery contracts, and the quantitative evidence pipeline.

## Project contributions

| Contribution | Design | Practical result |
| --- | --- | --- |
| Independent views and counterevidence | Separate user expectations from research inputs, seal bull/bear drafts, then record responses to each claim | The report explains which beliefs changed, why they changed and what evidence would reopen them |
| Operating assumptions to valuation | Store revenue, margins, tax, attribution, shares and cash adjustments; apply explicit corrections and recompute | Earnings, EPS, cash and price requirements follow one effective parameter set |
| Evidence-linked delivery | Bind forecast quantities to calculations and historical quantities to source blocks; retain original proposals and responses | Readers can trace a report statement to its input, calculation or source |
| Recovery and runtime efficiency | Use bounded retries, stage-specific reasoning, shared prompt prefixes, targeted sentence repair and pending-number delivery | Completed work, failed calls, corrections and usage remain available for review and continuation |
| Quantitative research and integration | Rebuild dated stock universes, compare models under fixed splits and convert frozen scores into research notes | Cross-sectional ranking evidence enters the same source bundle as financial research |

## The financial research chain

```text
research question + company evidence + substantive hypotheses
   → fundamental, market, news and sentiment analysis
   → independent bull/bear drafts and claim-by-claim counterevidence
   → operating scenarios and forecast parameters
   → program calculations and explicit parameter corrections
   → effective earnings, cash, valuation and return scenarios
   → report, belief updates and evidence that would change the judgment
```

The workflow distinguishes operating performance, profit attribution and cash generation. This makes a forecast revision financially interpretable: a change in minority attribution affects parent earnings and EPS, while consolidated cash can remain unchanged.

[Financial workflow](docs/thesis-research.md) · [Architecture and interfaces](docs/architecture.md) · [Quant pipeline](quant/README.md)

### Quant results: final period 2024–2025

The preregistered study compared annual refits over 485 trading days and 2,424,608 stock-days. Forecasts were persisted and replayed before performance was revealed. Mean Rank IC identification bounds:

| Model | Stock-only | With market context |
| --- | ---: | ---: |
| Ridge | 0.1147–0.1162 | 0.1234–0.1249 |
| XGBoost | 0.1241–0.1256 | 0.1329–0.1344 |
| GRU | 0.1091–0.1106 | 0.1353–0.1366 |

All six models had positive annual mean IC bounds. Market context raised the mean bounds in all three model families in this final period. The study reports missing-outcome identification bounds and HAC uncertainty separately. [Full results and comparisons](docs/status.md#previous-slice-completed-fixed-research-study--2026-09-22)

## Financial correction example

The synthetic offline example corrects the direction of minority-profit attribution, applies the change and regenerates the report from the effective calculations.

| Metric | Before | After |
| --- | ---: | ---: |
| EPS | 1.70 | 1.30 |
| Consolidated operating cash flow | 16.00 | 16.00 |

EPS changes because minority profit is deducted from consolidated earnings. Consolidated cash flow retains its original base. A second control exercises the report's treatment of an unbound forecast quantity.

[Run the example](#try-the-current-research-workflow-offline) · [Calculation and report contracts](docs/thesis-research.md)

## A-share quantitative research

The public V2/V3 extension adds exact historical neighbours, learned numerical/cosine
distance, a separate future-loss estimate, dated calibration and G0/G1 mixtures.
NN here means nearest neighbours. The optional launcher and synthetic numerical
checks are included; [definitions and commands](docs/quant-research.md) and
[latest research results](docs/status.md#current-quant-publication-v2v3-modules--2026-10-07)
explain the scope and measured increments.

The [Quant module](quant/README.md) studies which price-volume paths persist or reverse under different market states, and how sequence models compare with engineered features and trees.

| Stage | Technical design |
| --- | --- |
| Data | Daily records in a normalized private store; dated security identities and historical stock universes |
| Features | 60-session price-volume paths, trend, risk, activity, liquidity proxies, market state and relative strength |
| Labels | Next-session-open to 20th-session-close holding-return ranks; unknown outcomes retained as rank intervals |
| Splits and models | Date-level label-end and availability purges, train-only preprocessing, Ridge/XGBoost/GRU with two input groups |
| Evaluation | Full-pool Rank IC identification bounds, HAC20/60, fixed regimes and recomputation from saved daily metrics |
| Agent integration | Date-bounded scoring, input fingerprints and a deterministic research note added to the source bundle |

The A–D protocol fixes data admission, training-window selection, development candidates and final-period evaluation before results are read. Public code contains the contracts, data processing, XGBoost adapter and synthetic checks; the private workspace retains the real-data runs and Ridge/GRU fitters.

## TradingAgents research integration

Use [`research-thesis`](docs/thesis-research.md) for the main workflow. It keeps
the native analyst/research/trader/risk/portfolio topology and routing while
changing role prompts and information flow. Complete native tool returns, or a
frozen source bundle, reach the research roles without the old field projection.
From protocol 23 the final report always gives one of five ratings with a confidence; earlier protocols allowed `REVIEW` when there was no defensible rating.
Each newly saved Case includes a formal research report (`research-report.md` / `.html`)
laid out like a conventional company report, with numbered citations and a compact
appendix; the complete workpaper and process record stay alongside it for review.
Earlier v16-v24 Cases can add it offline with `render-research-report`.

Use `--fetch-news-social --sources <base.json>` to acquire dated A-share news/events
and public investor discussions before the four-analyst flow. The supplied base
retains financial, market and Quant inputs. The collector labels media excerpts,
company events and investor Q&A separately, preserves coverage gaps, and freezes
the combined sources for replay. See the [workflow guide](docs/thesis-research.md)
for source coverage and continuation behavior.

The repository also retains `tradingagents-baseline` for native comparison,
a [filing-focused component](docs/tradingagents-research.md), and a
[typed native audit route](docs/native-audit.md). Each has its own input and report
contract; the typed route labels Hold as an interface-compatibility value.
Model-backed routes share a separately installed integration environment.

## Try the current research workflow offline

This example follows `research-thesis`, including a scripted parameter correction,
program recomputation, final-report number references, saving and reopening.
It uses scripted synthetic responses and blocked external network paths to
exercise the parameter, calculation and delivery mechanisms.

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
EPS changes from 1.70 to 1.30 while consolidated operating cash flow remains 16.00.
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
The command uses the filing and model already available in the local environment.

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

## Verification and research evidence

The offline suite exercises financial rules, input contracts, private storage,
Application persistence and replay. Research evaluations record their own inputs,
comparators and outcomes in [`docs/status.md`](docs/status.md), using the
[evaluation protocol](docs/evaluation-protocol.md).

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
raw model traces, and manual QA live in the sibling `private/` directory. See [`docs/data-policy.md`](docs/data-policy.md) and
[`NOTICE_DATA.md`](NOTICE_DATA.md).

## License

Original code, documentation, schemas, and synthetic fixtures are licensed
under the [Apache License 2.0](LICENSE). Third-party filings, issuer names and
marks, model weights, and evaluation material are not relicensed.
