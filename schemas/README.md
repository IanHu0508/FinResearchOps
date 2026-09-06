# Persisted artifacts and their schemas

Every artifact the code reads or writes has exactly one schema version. When a
shape changes, that artifact's version is bumped and the old branch is deleted;
older numbers exist only in Git history and the archived pre-simplification
tree. Version numbers are monotonic and may have gaps.

JSON Schema files exist for the artifacts that cross the Application or
public boundary. Core run artifacts are defined by the code that writes and
replays them (`src/finauditgate/core/engine.py`).

## TradingAgents research artifacts

| Artifact | Schema | Definition |
|---|---|---|
| core `research-evidence.json` and manifest | `finauditgate.research-evidence/v2` | `core/research_evidence.py`, adds explicit disclosed-amount normalization and program-computed cash/profit ratios |
| evidence replay response | `finauditgate.research-evidence-replay/v1` | deterministic source, calculation and lookup replay |
| `research-cases/<ref>/case.json` | `finresearchops.investment-research-case/v5` | `investment-research-case.v5.schema.json`; compact audited inputs with explicit use limits, qualitative prose and evidence conditions; final synthesis excludes initial opinion text |
| `baseline-cases/<ref>/case.json` | `finresearchops.tradingagents-baseline/v1` | `tradingagents-baseline.v1.schema.json`; native reports explicitly marked unaudited |
| `research-executions/*/new-decision.json` | `finresearchops.new-research-decision/v1` | decision saved before the old Case is read |
| research failure record | `finresearchops.research-failure/v1` | core references and a bounded failure code |
| native failure record | `finresearchops.tradingagents-baseline-failure/v1` | exception type and budget receipt, no credential or exception-body serialization |
| product model request/response/error | `finresearchops.research-model-request/v1`, `finresearchops.research-model-response/v3`, `finresearchops.research-model-error/v1` | private JSON-mode model records, including finish reason |
| native model request/response/error | `finresearchops.native-model-request/v1`, `finresearchops.native-model-response/v2`, `finresearchops.native-model-error/v1` | private upstream callback records; response includes finish reason |
| `wire-<n>.json` | `finresearchops.model-wire/v2` | actual model, output limit and reasoning-effort fields after SDK translation; no credentials or HTTP headers |

Research HTML and native Markdown are renderings recomputed on Case reopen.
Raw model traces remain private; reopening verifies captured stage inputs and
proposals, not a new stochastic model response or hidden model reasoning.

## Cash-flow investigation

This task uses its own artifact shapes and the same `run/replay` Interface.
It does not supply a reviewed-answer policy to the older task type.

| Artifact | Schema | Definition |
|---|---|---|
| `runs/<id>/cashflow.json` and its `manifest.json` | `finauditgate.cashflow-run/v3` | `core/cashflow.py`; source metadata, financial facts, classified source passages, actions with model/rule origin, and captured responses |
| financial analysis within the record | `finresearchops.cashflow-analysis/v3` | Adds explicit-source resolutions, the operating-assets/liabilities section, overview and disclosed-amount comparison |
| replay response | `finauditgate.cashflow-replay/v1` | Offline recalculation and recorded-action verification |
| `application/cashflow-cases/<ref>/case.json` | `finresearchops.cashflow-case/v1` | `cashflow-case.v1.schema.json` |
| `workpaper.html`, `evidence.html` | renderings, not JSON artifacts | Hash-bound to the Case and regenerated from the core record when reopening |

Full behavior and limits: [cash-flow investigation](../docs/cashflow-investigation.md).

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
