# FinResearchOps / FinAuditGate

A local investment-research workflow built on the pinned TradingAgents graph,
with independent first drafts, explicit counterevidence updates and a fresh
final assessment. A separate, optional data-review Agent checks the saved report.

## Why this project

项目重点是避免初始投资倾向一路变成最终结论。多空首轮各自成稿，第二轮
对称回应对方初稿，记录每条论点维持、修改、撤回或未解决的原因；最终经理
从原始资料、更新论据和风险分析重新判断，不接收前序评级、交易方向或交易员自设门槛。
资料复核放在主体报告保存之后，其失败或不同意见不会改写主结论。

The workflow addresses two sources of research error:

- **Opinion propagation:** independent drafts, itemized revision reasons and final judgment without upstream ratings.
- **Financial inputs:** complete source access and optional review of dates, currencies, measurement and unsupported inferences.

This is a research prototype for human review. It does not establish investment
performance, eliminate model bias, or provide a complete valuation system.

FinAuditGate remains available for standalone filing and cash-flow checks. Its
limited coverage is not a prerequisite for the main research path. Neither
source references nor agreement among agents establish financial truth.

Current scope, evidence and remaining work are maintained only in
[`docs/status.md`](docs/status.md).

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
