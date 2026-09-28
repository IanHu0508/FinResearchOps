# `finresearchops` CLI

The CLI is a thin Adapter over the Application Interface. Every action is one
call to `handle()` or `read_case()`; the CLI carries no financial, gating,
review, or export rule of its own.

```text
finresearchops --artifact-root <private root> <action> [options]
```

| Action | Application call | Caller-supplied fields |
|---|---|---|
| `research-thesis` | `handle(ResearchThesis)` | `--symbol`, `--as-of`, `--question`, optional `--sources`, `--all-analysts`, `--fetch-news-social`, `--no-review`, horizon and model options; see [main workflow](thesis-research.md) |
| `tradingagents-baseline` | `handle(RunTradingBaseline)` | `--symbol`, `--as-of`, optional `--env-file`, `--model`, `--max-spend-cny` |
| `research-security` | `handle(ResearchSecurity)` | acquired filing options plus `--symbol`, `--question`, optional `--horizon-months`, `--previous-case-ref`, model options |
| `investigate-cashflow` | `handle(InvestigateCashflow)` | acquired `--source-manifest` or explicit source metadata, `--comparison-end`, `--cutoff`, `--currency`, `--strategy rules/adaptive` |
| `create-case` | `handle(CreateCase)` | `--question`, `--cutoff`, `--document`, `--source-id`, `--published-at`, `--mode` (`SYNTHETIC_DEV` / `PRIVATE_DEV`), `--risk-class`, optional `--document-name` |
| `run-analysis` | `handle(RunAnalysis)` | `--case-ref`, `--model-trace-root`, optional `--validation-profile` |
| `inspect-case` | `read_case(case_ref)` | `--case-ref`, optional `--model-trace-root` |
| `render-research-report` | `handle(RenderResearchReport)` | `--case-ref` of a saved v16-v20 thesis Case, optional `--model-trace-root`; writes the formal report offline |
| `review` | `handle(SubmitReview)` | `--case-ref`, `--run-id`, `--action` (`APPROVE` / `RETURN` / `REJECT`), `--reason`, optional `--model-trace-root` |
| `export` | `handle(ExportChangePacket)` | `--case-ref`, `--run-id`, optional `--model-trace-root` |
| `replay` | `handle(ReplayRun)` | `--run-id`, optional `--model-trace-root` |

Cash-flow command details and limits are in [cashflow-investigation.md](cashflow-investigation.md).
TradingAgents setup, both new commands, and their data/model boundaries are
described in [tradingagents-research.md](tradingagents-research.md).
It writes a local draft awaiting human review; its approval/public-export path
is not implemented. `inspect-case` and `replay` also accept its references.

Commands never accept a machine decision, an answer, a verified fact, a
calculation, `proposal_only=false`, or a resulting Case status. The Application
derives those from the core artifacts and its append-only Review history.

## Runtime boundary

- `research-thesis --all-analysts` (or `--fetch-news-social`) starts protocol 20 for a
  new execution. `--resume-execution` keeps the protocol recorded in that execution's
  runtime receipt (16 to 20) and refuses one that does not match the analyst options.
  A protocol 20 execution whose final report already used its sentence-level number
  repair is not resumed; start a new execution instead.
- `research-thesis --fetch-news-social --sources <base.json>` automatically adds
  bounded dated A-share news/events and public investor discussions to the supplied
  financial, market and Quant foundation before running all four analysts.
  It persists the acquisition receipt and merged source snapshot in the private
  execution directory. Keep this flag on a `--resume-execution` invocation: the
  saved acquisition is reused without fetching again. If an interrupted resume
  has no runtime checkpoint, resume from the original valid execution instead;
  a copied source receipt cannot reset paid calls or their budget.
- `--artifact-root` fixes the private workspace anchor. `--model-trace-root`,
  `--validation-profile`, and a `PRIVATE_DEV --document` must resolve below
  the same sibling `private/` tree. Relative paths, public-worktree paths, and
  symbolic-link escapes fail with a stable, path-free error.
- `SYNTHETIC_DEV` may read the public synthetic fixture.
- `investigate-cashflow --strategy adaptive` constructs the separate bounded
  local planner; `--strategy rules` has no model call.
- The profile-based `run-analysis` constructs its candidate model Adapter. It requires
  `--model-trace-root` and, to accept a `PRIVATE_DEV` case, a
  `--validation-profile`. The local model reads at most 32,768 bytes of UTF-8
  text; the CLI does not parse PDFs.
- `inspect-case`, `review`, `export`, and `replay` reopen the Application
  offline. Pass `--model-trace-root` when the run was traced so its trace can
  be verified during replay.
- `research-thesis` and `inspect-case` report `research_report` when the formal
  report exists; `delivery_report` then points to it unless an effective automatic
  correction is delivered instead. `render-research-report` verifies the saved Case,
  makes no model or network call, adds only `research-report.md/.html` (in the version
  of an interrupted HTML file, otherwise the current format), leaves a complete saved
  report unchanged, and refuses to replace a different existing file.
- Successful output is one line of canonical compact JSON on stdout. Failures
  are one `finresearchops.cli-error/v1` JSON object on stderr and exit code 2.

Package entry point: `finresearchops = finauditgate.cli:main`.
