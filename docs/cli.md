# `finresearchops` CLI

The CLI is a thin Adapter over the Application Interface. Every action is one
call to `handle()` or `read_case()`; the CLI carries no financial, gating,
review, or export rule of its own.

```text
finresearchops --artifact-root <private root> <action> [options]
```

| Action | Application call | Caller-supplied fields |
|---|---|---|
| `create-case` | `handle(CreateCase)` | `--question`, `--cutoff`, `--document`, `--source-id`, `--published-at`, `--mode` (`SYNTHETIC_DEV` / `PRIVATE_DEV`), `--risk-class`, optional `--document-name` |
| `run-analysis` | `handle(RunAnalysis)` | `--case-ref`, `--model-trace-root`, optional `--validation-profile` |
| `inspect-case` | `read_case(case_ref)` | `--case-ref`, optional `--model-trace-root` |
| `review` | `handle(SubmitReview)` | `--case-ref`, `--run-id`, `--action` (`APPROVE` / `RETURN` / `REJECT`), `--reason`, optional `--model-trace-root` |
| `export` | `handle(ExportChangePacket)` | `--case-ref`, `--run-id`, optional `--model-trace-root` |
| `replay` | `handle(ReplayRun)` | `--run-id`, optional `--model-trace-root` |

Commands never accept a machine decision, an answer, a verified fact, a
calculation, `proposal_only=false`, or a resulting Case status. The Application
derives those from the core artifacts and its append-only Review history.

## Runtime boundary

- `--artifact-root` fixes the private workspace anchor. `--model-trace-root`,
  `--validation-profile`, and a `PRIVATE_DEV --document` must resolve below
  the same sibling `private/` tree. Relative paths, public-worktree paths, and
  symbolic-link escapes fail with a stable, path-free error.
- `SYNTHETIC_DEV` may read the public synthetic fixture.
- Only `run-analysis` constructs the model Adapter. It requires
  `--model-trace-root` and, to accept a `PRIVATE_DEV` case, a
  `--validation-profile`. The local model reads at most 32,768 bytes of UTF-8
  text; the CLI does not parse PDFs.
- `inspect-case`, `review`, `export`, and `replay` reopen the Application
  offline. Pass `--model-trace-root` when the run was traced so its trace can
  be verified during replay.
- Successful output is one line of canonical compact JSON on stdout. Failures
  are one `finresearchops.cli-error/v1` JSON object on stderr and exit code 2.

Package entry point: `finresearchops = finauditgate.cli:main`.
