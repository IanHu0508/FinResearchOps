# FinResearchOps CLI Contract Draft

> Status: `SCHEMA_DRAFT`. The CLI and FinResearchOps Application Module are
> `NOT_STARTED`; this document is not runnable behavior.

The future CLI is a thin Adapter over the same Application Interface used by
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
`proposal_only=false`, resulting Case status, or export eligibility. The future
Application must derive those values from the referenced FinAuditGate artifacts
and its append-only Review history.

`EXPORT_CHANGE_PACKET` must remain ineligible until the M2 Application can prove
that the selected run, Workpaper, and `APPROVE` Review belong to the same Case.
M1 schema patterns and the synthetic mapping example do not implement that
cross-object rule.

No interactive UI is part of this draft. A later UI must call the same two
Application methods and may not introduce a second business-rule path.
