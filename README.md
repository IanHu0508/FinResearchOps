# FinResearchOps / FinAuditGate

A local financial-research workflow for investigating earnings and operating
cash flow after an annual filing. **Programs read and calculate financial
facts; a bounded agent chooses what to investigate; people review the evidence.**

## Why this project

投研判断需要两端约束：先核对财务事实，再让新增证据影响判断。本项目从年度
盈利与经营现金流调查切入，用确定性核验、受限附注补查和独立证据综合，形成
可追溯的中文研究草稿。最终综合不接收初稿观点文字，旧观点在新判断保存后才
参与对照；这些机制并不证明模型偏好已经消失。

The workflow addresses two sources of research error:

- **Financial inputs:** source, period, currency and calculation checks before interpretation.
- **Opinion propagation:** separate initial drafts, bounded counter-evidence lookup, and final synthesis from audited evidence rather than draft conclusions.

This is a research prototype for human review. It does not establish investment
performance, eliminate model bias, or provide a complete valuation system.

The first user path reads an acquired inline-XBRL filing, compares consolidated
profit and operating cash flow, locates major reconciliation items, searches
related disclosures and writes a Chinese draft workpaper. A rule baseline and
a fixed local 8B planner use the same financial checks and search budget.
The task requires no per-question answer profile.

Current scope, evidence and remaining work are maintained only in
[`docs/status.md`](docs/status.md).

## TradingAgents research integration

The optional integration offers a native upstream baseline and a slim research
route that combines audited cash-flow evidence, isolated analysis/challenge
drafts, source-aware synthesis and old-thesis comparison. It uses a separate
dependency environment. Setup, CLI commands and limitations are in
[`docs/tradingagents-research.md`](docs/tradingagents-research.md).

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
