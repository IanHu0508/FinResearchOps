# `finresearchops` CLI

The CLI is a thin Adapter over the Application Interface. Every action is one
call to `handle()` or `read_case()`; the CLI carries no financial, gating,
review, or export rule of its own.

```text
finresearchops --artifact-root <private root> <action> [options]
```

| Action | Application call | Caller-supplied fields |
|---|---|---|
| `tradingagents-baseline` | `handle(RunTradingBaseline)` | `--symbol`, `--as-of`, optional `--env-file`, `--model`, `--max-spend-cny` |
| `research-security` | `handle(ResearchSecurity)` | acquired filing options plus `--symbol`, `--question`, optional `--horizon-months`, `--previous-case-ref`, model options |
| `investigate-cashflow` | `handle(InvestigateCashflow)` | acquired `--source-manifest` or explicit source metadata, `--comparison-end`, `--cutoff`, `--currency`, `--strategy rules/adaptive` |
| `create-case` | `handle(CreateCase)` | `--question`, `--cutoff`, `--document`, `--source-id`, `--published-at`, `--mode` (`SYNTHETIC_DEV` / `PRIVATE_DEV`), `--risk-class`, optional `--document-name` |
| `run-analysis` | `handle(RunAnalysis)` | `--case-ref`, `--model-trace-root`, optional `--validation-profile` |
| `inspect-case` | `read_case(case_ref)` | `--case-ref`, optional `--model-trace-root` |
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
- Successful output is one line of canonical compact JSON on stdout. Failures
  are one `finresearchops.cli-error/v1` JSON object on stderr and exit code 2.

Package entry point: `finresearchops = finauditgate.cli:main`.
