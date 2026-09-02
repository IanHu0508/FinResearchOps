# Persisted artifacts and their schemas

Every artifact the code reads or writes has exactly one schema version. When a
shape changes, that artifact's version is bumped and the old branch is deleted;
older numbers exist only in Git history and the archived pre-simplification
tree. Version numbers are monotonic and may have gaps.

JSON Schema files exist for the artifacts that cross the Application or
public boundary. Core run artifacts are defined by the code that writes and
replays them (`src/finauditgate/core/engine.py`).

## FinAuditGate run directory (`runs/<run_id>/`)

| File | Schema version | Contents |
|---|---|---|
| `task.json` | `finauditgate.task/v1` | the normalized `AuditTask` (`run-task-artifact.v1.schema.json`) |
| `candidate.json` | `finauditgate.candidate/v2` or `null` | the last well-formed candidate |
| `attempts.json` | `finauditgate.attempts/v2` | every proposal snapshot, candidate, disposition, and reason codes |
| `policy.json` | `finauditgate.calculation-policy/v2` or `finauditgate.private-dev-validation-profile/v2` | the frozen profile the run was validated against |
| `ledger.json` | `finauditgate.ledger/v2` | verified evidence nodes, or the rejection record |
| `formula.json` | `finauditgate.formula/v2` | operand lineage, Decimal context, result, or the non-execution record |
| `outcome.json` | `finauditgate.run/v1` | the returned `AuditOutcome` (`audit-outcome.v1.schema.json`) |
| `identity.json` | `finauditgate.run-identity/v5` | hashes of task, candidate, attempts, model-trace summary (nullable), policy; its SHA-256 is the run id |
| `model-trace.json` | `finauditgate.model-trace-summary/v3` | per-attempt trace receipts (traced runs only) |
| `manifest.json` | `finauditgate.manifest/v2` | filename and SHA-256 of every artifact above |

`replay()` returns `finauditgate.replay/v5` (`replay-report.v5.schema.json`).

## Private model traces (`<trace root>/model-calls/sha256/<sha>.json`)

| Artifact | Schema version |
|---|---|
| raw trace (request, base64 response, HTTP status, capture flag, metrics) | `finauditgate.model-call-trace/v6` |
| receipt embedded in the run's trace summary | `finauditgate.model-trace-receipt/v3` |

## FinResearchOps Case directory (`application/cases/<case_ref>/`)

| File | Schema | JSON Schema file |
|---|---|---|
| `case.json` | `finresearchops.case-record/v1` | `case-record.v1.schema.json` |
| `journal-head.json` | `finresearchops.case-journal-head/v1` | `case-journal-head.v1.schema.json` |
| `transactions/*.intent.json` | `finresearchops.case-transaction/v1` | `case-transaction.v1.schema.json` |
| `transactions/*.commit.json` | `finresearchops.case-transaction-commit/v1` | `case-transaction-commit.v1.schema.json` |
| `workpapers/*.json` | `finresearchops.workpaper/v3` | `workpaper.v3.schema.json` |
| `reviews/*.json` | `finresearchops.review-record/v1` | `review-record.v1.schema.json` |
| `packets/*.json` | `finresearchops.research-change-packet/v1` | `research-change-packet.v1.schema.json` |
| `replays/*.json` | `finresearchops.replay-record/v3` | `replay-record.v3.schema.json` |

`run-ref.v1.schema.json` is the shared `{run_id}` reference.

## Paired evaluation (`paired/`)

| Artifact | Schema version |
|---|---|
| execution pair | `finresearchops.paired-execution/v2` |
| human QA record | `finresearchops.paired-human-qa/v1` |
