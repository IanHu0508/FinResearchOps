# FinResearchOps CLI Contract Draft

> Status: `SCHEMA_DRAFT`. The product CLI is `NOT_STARTED`. The M2 scripted
> FinResearchOps Application Interface is implemented, but this document is not
> runnable CLI behavior.

The planned CLI is a thin Adapter over the same Application Interface used by
tests and any later UI:

```text
handle(command) -> application_outcome
read_case(case_ref) -> case_view
```

It must not call FinAuditGate internals, calculate answers, mark evidence
verified, choose a machine decision, or write Case/Review/Packet state directly.

## Closed command vocabulary

| CLI intent | Application call | Caller-supplied fields |
|---|---|---|
| create a Case | `handle(CREATE_CASE)` | `question`, `cutoff`, submitted immutable document package |
| run analysis | `handle(RUN_ANALYSIS)` | `case_ref` |
| inspect Case and Workpaper | `read_case(case_ref)` | `case_ref` |
| submit human review | `handle(SUBMIT_REVIEW)` | `case_ref`, `run_ref`, `action`, `reason` |
| export a proposal | `handle(EXPORT_CHANGE_PACKET)` | `case_ref`, `run_ref` |
| replay a run | `handle(REPLAY_RUN)` | `run_ref` |

`action` is exactly `APPROVE`, `RETURN`, or `REJECT`. Commands never accept
caller-provided `ACCEPT`, answer values, verified facts, calculations,
`proposal_only=false`, resulting Case status, or export eligibility. The
Application derives those values from the referenced FinAuditGate artifacts
and its append-only Review history.

`EXPORT_CHANGE_PACKET` is ineligible unless the Application proves that the
selected latest run, Workpaper, replay result, and `APPROVE` Review belong to
the same Case. This M2 rule and its foreign-artifact/recovery regressions pass
the renewed full-M2 Gate. The product CLI remains `NOT_STARTED`; a future CLI
may only request the command and may not reproduce or weaken the check.

No interactive UI is part of this draft. A later UI must call the same two
Application methods and may not introduce a second business-rule path.
