# Persisted artifacts and their schemas

Every artifact the code reads or writes has exactly one schema version. A shape
change bumps that artifact's version. Superseded, unreleased formats need no
compatibility branch; released formats retain the read/replay behavior documented
below so historical records are not rewritten. Version numbers are monotonic
and may have gaps.

JSON Schema files exist for the artifacts that cross the Application or
public boundary. Core run artifacts are defined by the code that writes and
replays them (`src/finauditgate/core/engine.py`).

## TradingAgents research artifacts

The main research path writes `finresearchops.thesis-case/v11`
(`thesis-case.v11.schema.json`); published v1/v2/v10 remain readable without rewriting
their reports. V10 retains the v9 model-proposed forward draft and deterministic
earnings/cash/conditional-return results that reach the final judge before its
rating. Forecast assumptions and scenario acceptance are visible separately.
It retains compact independent underwriting, belief-by-belief counterevidence
updates, adopted assumptions and a program-computed rating comparison. The
initial rating/summary and trigger fields are not final-judge inputs. Unreleased
v3/v4/v5/v6/v7/v8/v9 trials and their original readers remain private development evidence.
V2 introduced financial analysis covering operations, earnings quality,
cash/capital allocation and valuation/price requirements, with source references.
Financial prose and forecast assumptions remain model analyses; the deterministic
calculator establishes arithmetic, not economic correctness. The Case stores original source text, independent
initial claims, symmetric revisions, fresh assessments and actual model I/O.
`finresearchops.thesis-sources/v2` describes the frozen input bundle with an
explicit research/sensitivity use per source. Sensitivity notes are withheld
from every rating request and displayed in a clearly labeled report appendix;
their original text remains in the Case and reaches only post-report review. Legacy v1 inputs are accepted as unclassified research sources;
`finresearchops.thesis-review/v1` is a separately saved post-report Agent opinion.
Review state does not change the main Case hash. `finresearchops.thesis-runtime/v1`
captures partial or complete model/tool/node activity and budget, including
interrupted requests. The artifact validators establish protocol/receipt
integrity, not financial truth or absence of model bias.
`finresearchops.thesis-failure/v1` records bounded failure codes and exception
type chains without copying provider error bodies. Completed model returns can
be reused after exact-message checks; nested `prior_reuse` receipts preserve
earlier reuse and cost history. A narrowly supported manager Markdown layout is
read verbatim, with no default rating and no financial corrections.

The native audited path uses `finresearchops.native-audited-case/v1`
(`native-audited-case.v1.schema.json`) for offline cases: core reference, research task, native
topology, actual model/tool/node records and bound native reports. Its
`finresearchops.native-audit-packet/v1` is reconstructed from the core record
on reopen; no new model execution is required. Failures use
`finresearchops.native-audit-failure/v1`. Live Case v2 adds a replayable security/market core reference and model budget. Source-filtered packet v3 was used by a rejected run, without a persisted Case v3. Case/packet v4 binds retained vendor returns to projected tool responses, refines signed-adjustment, tax-period and horizon rules, and adds a source-derived financial table to the report. See [native-audit.md](../docs/native-audit.md).

| Artifact | Schema | Definition |
|---|---|---|
| native typed-judgment Case | `finresearchops.native-audited-case/v5` | `native-audited-case.v5.schema.json`; core catalog and per-node review references, actual model-message binding, checked report projections |
| native typed propositions and adjudications | `finauditgate.native-judgment/v1` | `core/judgment.py`; source-bound catalog, bounded facts/hypotheses, requested/effective manager choices and recursive replay of prior reviews |
| core `research-evidence.json` and manifest | `finauditgate.research-evidence/v3` | `core/research_evidence.py`; normalized amounts, ratios, and core-owned measurement/period meanings for financial references |
| interim core evidence and manifest | `finauditgate.research-evidence/v5` | Half-year source and anchors, separate balances/tax, profit-change bridge and period-filtered notes; delivered v4 retains its original replay behavior |
| native interim earnings attribution | `finauditgate.research-evidence/v6` | Explicit owner-earnings scope adds signed consolidated-to-parent reconciliation; existing evidence keeps its original replay |
| security and market core | `finauditgate.security-market/v1` | `security-market.v1.schema.json`; source-bound ADS identity, quote/snapshot/processed OHLCV checks and frozen input hashes |
| live native request/response | `finresearchops.native-live-request/v1`, `finresearchops.native-live-response/v1` | Actual messages, finish reason and usage; private and persisted per request |
| live native runtime receipt | `finresearchops.native-live-runtime/v1` | Partial or complete model/tool/node records and budget; corrected runtime receipt v2 adds vendor observations |
| filtered financial tool response | `finresearchops.native-financial-tool-view/v1` | Source/period coverage, checked fields, original vendor hash and explicit missing data |
| evidence replay response | `finauditgate.research-evidence-replay/v1` | deterministic source, calculation and lookup replay |
| `research-cases/<ref>/case.json` | `finresearchops.investment-research-case/v5` | `investment-research-case.v5.schema.json`; compact audited inputs with explicit use limits, qualitative prose and evidence conditions; final synthesis excludes initial opinion text |
| interim `research-cases/<ref>/case.json` | `finresearchops.investment-research-case/v6` | `investment-research-case.v6.schema.json`; source/period comparison and whether new material was already public at the prior cutoff |
| new interim `research-cases/<ref>/case.json` | `finresearchops.investment-research-case/v7` | `investment-research-case.v7.schema.json`; adds a post-decision explanation covering each prior claim, with current evidence references and cumulative call budget |
| `baseline-cases/<ref>/case.json` | `finresearchops.tradingagents-baseline/v1` | `tradingagents-baseline.v1.schema.json`; native reports explicitly marked unaudited |
| `research-executions/*/new-decision.json` | `finresearchops.new-research-decision/v1` | decision saved before the old Case is read |
| research failure record | `finresearchops.research-failure/v1` | core references and a bounded failure code |
| native failure record | `finresearchops.tradingagents-baseline-failure/v1` | exception type and budget receipt, no credential or exception-body serialization |
| product model request/response/error | `finresearchops.research-model-request/v1`, `finresearchops.research-model-response/v4`, `finresearchops.research-model-error/v1` | private JSON-mode records, raw content, finish reason and any exact schema-title-echo normalization |
| native model request/response/error | `finresearchops.native-model-request/v1`, `finresearchops.native-model-response/v2`, `finresearchops.native-model-error/v1` | private upstream callback records; response includes finish reason |
| `wire-<n>.json` | `finresearchops.model-wire/v2` | actual model, output limit and reasoning-effort fields after SDK translation; no credentials or HTTP headers |

Research HTML and native Markdown are renderings recomputed on Case reopen.
The publicly released evidence v2 remains readable and replayable without v3
measurement metadata; annual runs write v3, ordinary interim runs v5, native owner-earnings runs v6. Case v5 is unchanged in shape and
uses the corresponding evidence version to reconstruct its captured input and HTML.
Raw model traces remain private; reopening verifies captured stage inputs and
proposals, not a new stochastic model response or hidden model reasoning.

## Cash-flow investigation

This task uses its own artifact shapes and the same `run/replay` Interface.
It does not supply a reviewed-answer policy to the older task type.

| Artifact | Schema | Definition |
|---|---|---|
| `runs/<id>/cashflow.json` and its `manifest.json` | `finauditgate.cashflow-run/v3` | `core/cashflow.py`; source metadata, financial facts, classified source passages, actions with model/rule origin, and captured responses |
| financial analysis within the record | `finresearchops.cashflow-analysis/v3` | Adds explicit-source resolutions, the operating-assets/liabilities section, overview and disclosed-amount comparison |
| interim financial analysis | `finresearchops.cashflow-analysis/v4` | Reuses the decomposition, adds explicit half-year coverage and separate cash-tax, tax-expense and instant-balance fields |
| new interim financial analysis | `finresearchops.cashflow-analysis/v5` | Adds signed profit contributions, pretax/net-income/tax cross-checks and within-period explanation retrieval |
| native owner-earnings analysis | `finresearchops.cashflow-analysis/v6` | Adds the signed consolidated-to-parent earnings reconciliation, without EPS/FX/ADS annualization |
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

V11 retains the original forward proposal and adds one explicit revision, effective
inputs/computation, program-bound metric references and final-generation recovery
records. The process appendix is separate from current conclusions. V10 remains
a published compatibility format; its reader and rendering are unchanged.
