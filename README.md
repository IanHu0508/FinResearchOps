# FinResearchOps

**An auditable multi-agent research workflow on TradingAgents in which forward numbers are recomputed by the program and every model step can be re-proven from saved evidence, together with a preregistered A-share quantitative research study whose frozen signal reaches the agents as citable evidence.**

The upstream TradingAgents graph and role topology are kept. This project changes what each role sees, how opinions are revised and how financial numbers reach the report. Models propose; validators decide; people approve.

### Quant results: final period 2024–2025

Preregistered study with annual refits over 485 trading days and 2,424,608 stock-days; every forecast was persisted and replayed exactly before the one-way performance reveal. Mean Rank IC identification bounds:

| Model | Stock-only | With market context |
| --- | ---: | ---: |
| Ridge | 0.1147–0.1162 | 0.1234–0.1249 |
| XGBoost | 0.1241–0.1256 | 0.1329–0.1344 |
| GRU | 0.1091–0.1106 | 0.1353–0.1366 |

All six models had positive annual mean IC bounds, and market context raised every model family. The bounds reflect unknown outcomes; they are not confidence intervals or returns. [Full results](docs/status.md#previous-slice-completed-fixed-research-study--2026-09-22)

## Highlights

- **Independent drafts, then rebuttal.** Bull and bear researchers write first drafts from the same sources and then answer each other's sealed draft. The portfolio manager forms a source-only initial view, and the final assessment inherits no earlier rating or trader-defined threshold.
- **Numbers come from calculations or sources.** Forward assumptions are stored as parameters and recalculated in Python (attribution, EPS, cash bridge, conditional valuation). The final report cites them as `{{metric:F1:eps_per_traded_unit}}` and historical figures as evidence blocks such as `{{source:E0001}}`; a bare number in a value position is refused rather than saved.
- **Replayable Cases.** Each Case is content-addressed and keeps requests, raw model calls, failed answers and budget receipts. The Case reader re-proves every binding with the Python standard library only.
- **Bounded, proven recovery.** Transport failures, truncation and unparseable or schema-invalid answers each have a proof rule and a one-time allowance, decided identically at run time and on reopen. Protocol 20 continues past a non-critical analyst or trader failure with an explicit placeholder (the Case is saved PARTIAL) and rewrites at most five refused final-report sentences once; protocol 22 also asks once more when the program proves an inconsistent forward draft or an unknown source cited by a critical stage before the final report.
- **Every run ends with a conclusion.** From protocol 23 the final report always gives one of five ratings with a confidence, shown beside a rating the program recomputes from the scenarios with a fixed rule; a run that stops still saves a verifiable halted delivery with its completed stages and conclusion.
- **Cost-aware prompts.** Every stage before the final report sends the same fixed system message and starts with the same shared sources, so the provider's prefix cache can serve them; the final report shares its own prefix with its retries and repair, which are appended after the unchanged prompt. Reasoning effort is set per stage, and a truncated answer is asked again one level lower.
- **Preregistered A-share quant research.** Each date's universe is rebuilt from dated security identities rather than today's survivors. Sixty-session price-volume paths and market state predict 20-session forward-return ranks, with unknown outcomes kept as intervals, and purged date splits keep labels from leaking. Ridge, XGBoost and GRU are compared with and without market context in a four-stage preregistered study in which every final-period forecast is persisted and replayed exactly before performance is revealed. The frozen signal reaches the agents as a deterministic, citable research note.

Model ratings stay proposals until a person reviews them.

## Example: a financial correction changes EPS, not consolidated cash flow

The public offline demo applies a minority-interest correction through the current research path:

| Metric | Before | After |
| --- | ---: | ---: |
| EPS | 1.7 | 1.3 |
| Consolidated operating cash flow | 16 | 16 |

An intentionally unbound forecast number is rejected instead of being saved into the report. The example is synthetic; it shows how the workflow keeps report numbers tied to calculations.

[Run the offline demo](#try-the-current-research-workflow-offline) ·
[Research workflow](docs/thesis-research.md) ·
[Current status](docs/status.md)

## A-share quant research

The [Quant module](quant/README.md) asks which price-volume paths tend to persist or reverse under different market states, and whether sequence models add out-of-sample information beyond engineered features and tree models.

| Stage | What it does |
| --- | --- |
| Data | Raw daily acquisition into a normalized private store; security identities are resolved by date and historical universes are built as they were, without survivorship backfill |
| Features | 60-session price-volume paths; trend, risk, activity and liquidity proxies; contemporaneous market state and relative strength |
| Labels | Holding return from the next session's open to the 20th session's close, ranked within the complete eligible pool; unknown outcomes stay rank intervals |
| Splits and models | Date-level splits with label-end and availability purges; preprocessing fitted on training dates only; Ridge, XGBoost and GRU, each stock-only and with market context |
| Evaluation | Conservative full-pool Rank IC identification bounds, HAC intervals and fixed market-regime breakdowns, recomputed from persisted daily metrics |
| Agent integration | Date-bounded scoring reads only records up to the scoring date; versioned signals bind the fingerprint of the scoring input; a deterministic research note enters the agents as a citable source |

The preregistered study runs in four stages: data admission and freeze; a registered comparison of training windows decided by a preset Rank IC lower-bound rule; annual development candidates; and a final period with annual refits, in which every forecast is persisted and replayed exactly before a one-way performance reveal. Candidates, samples and directions are fixed before performance is seen. The complete results and research history are in [project status](docs/status.md#previous-slice-completed-fixed-research-study--2026-09-22).

This repository distributes the data, label, split and evaluation contracts, the XGBoost adapter and synthetic tests; real-data runs and the Ridge/GRU fitters stay in the private research workspace.

## TradingAgents research integration

Use [`research-thesis`](docs/thesis-research.md) for the main workflow. It keeps
the native analyst/research/trader/risk/portfolio topology and routing while
changing role prompts and information flow. Complete native tool returns, or a
frozen source bundle, reach the research roles without the old field projection.
From protocol 23 the final report always gives one of five ratings with a confidence; earlier protocols allowed `REVIEW` when there was no defensible rating.
Each newly saved Case includes a formal research report (`research-report.md` / `.html`)
laid out like a conventional company report, with numbered citations and a compact
appendix; the complete workpaper and process record stay alongside it for review.
Earlier v16-v23 Cases can add it offline with `render-research-report`.

Use `--fetch-news-social --sources <base.json>` to acquire dated A-share news/events
and public investor discussions before the four-analyst flow. The supplied base
retains financial, market and Quant inputs. The collector labels media excerpts,
company events and investor Q&A separately, preserves coverage gaps, and freezes
the combined sources for replay. See the [workflow guide](docs/thesis-research.md)
for coverage, limits and resume behavior.

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
