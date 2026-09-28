# Architecture

The main research path crosses `handle(ResearchThesis(...))` and `read_case`.
The optional `--all-analysts` frozen-source route uses protocol/Case v19 for new
runs (a resumed execution keeps the protocol it started with, so earlier v17 and
v18 executions resume as such): four actual analyst reports precede the existing
thirteen research stages. The
reports and original sources reach downstream research; the independent
assessment retains source-only input. It saves fundamental, market, news and
sentiment outputs plus explicit Quant/input-flow presentation. This route does
not invoke upstream live sentiment prefetch against historical dates. The
default v16 route and earlier recorded Cases retain their existing behavior.
The explicit `--fetch-news-social` route adds a bounded public-source collector
before that same four-analyst flow. It queries by instrument and cutoff, records raw
responses and typed coverage, then appends eligible news/events and investor
discussions to the unchanged financial/market/Quant foundation. Models consume
the frozen combined bundle; resume verifies and reuses it without fetching,
and the standard-library Case reader performs no acquisition. Missing channels
remain visible rather than being filled with invented material.
Protocol 18 decides every stage retry from the saved answer with a
standard-library validator and frozen stage schemas
(`adapters/thesis_schemas_v18.py`), identically at run time and on reopen. An
unparseable answer is asked again once with the unchanged prompt; a provenance
label outside its vocabulary (`basis_type`, `correction_basis`,
`update_basis`) is repaired alone while all other content is compared as
canonical JSON. Every other schema error, and every content, citation, coverage
or numeric failure, still halts the stage. Protocol 19 checks the frozen schema
before any content check, asks any other schema-invalid answer the validator
proves once more with the unchanged prompt, and drops blank `*_note` fields; its final instruction no longer asks to spell
numbers out, since Chinese number words with 百/千/万/亿 are refused as amounts.
Final-report number contract 2 (v18 and v19) only removes listed time and
source-identifier labels from contract 1's pending items; every refusal is
unchanged.
Its instance-local protocol keeps native graph routing while isolating initial
drafts, limiting round-two discussion to sealed first drafts, and obtaining
source-only independent underwriting before the portfolio manager sees peer
opinions. Within the same portfolio-manager node, a separate forward draft
proposes business-driven annual earnings/cash assumptions without seeing the
independent rating or beliefs. The core calculates the profit/attribution/EPS
and cash bridges, then conditional earnings-multiple prices and cumulative
returns. The final judge receives the exact draft and results alongside independent
beliefs and research/risk analysis, without the initial rating, summary or
prewritten trigger fields. It assesses each forecast path and updates each
belief; rating comparison is computed afterwards. Explicitly tagged
sensitivity notes appear only in the report appendix and post-report review,
never in a rating request. Untagged legacy
sources are not semantically filtered. Original vendor fields remain
available. The Application persists the main Case before the optional data
review and at most one targeted correction after the main Case is saved. The review reads the
complete structured report, research sources and effective calculations. Corrections are separate
artifacts; review failure retains the main report and marks the delivery rating unavailable. FinAuditGate is
an independent optional financial component, not this path's admission gate.
The calculator is an ordinary operating-company arithmetic tool, not a
valuation engine, assumption certifier or rating gate. Missing inputs preserve
the calculations that remain possible. Dates, earnings denominator and ADS/FX
units are explicit; no net cash is added to capitalized parent earnings. The
Application verifies that the final model received the recomputed results and
renders assumptions, source labels and final acceptance/rejection separately.
The current selected-evidence final call also receives a program-derived before/after
input comparison. The main report shows those objective changes, attribution directions
and unchanged dividends; model-written change and belief explanations remain complete
in the process appendix for review. This does not certify their economic reasoning or
remove the forward-calculation stages.
See [thesis-research](thesis-research.md). The mechanisms below describe retained
financial investigation and restricted audit routes.

Native audit execution crosses `handle(RunAuditedNativeResearch(...))` and
`read_case(...)`. The Native Adapter adds a fixed evidence block through the
upstream instance's instrument-context hook; original graph construction and
routing remain unchanged. Application validation binds the recorded model,
tool and node inputs/outputs to the core result and the final report. The live
tool adapter retains vendor returns and exposes only the core financial
projection to models. A separate core record binds ADS identity and market
inputs. See [native-audit.md](native-audit.md) for commands and limits.

The optional typed-judgment mode uses the same native workflow and state
factories. Its model Adapter submits source-bound propositions to the core
through `run(ReviewNativeJudgment(...))`. The core owns measurement types,
relations, supported hypothesis scopes and admissibility; the Application owns
review ordering and model/record/report binding. Only deterministic checked
projections enter downstream context and the report. Raw analyst prose remains
in private traces. This mode changes the judgment prompts; it is an enhancement,
not an untouched native baseline or a general natural-language verifier.

The interim research path uses `InterimCashflowTask` inside the same
`FundamentalEvidenceTask` and `ResearchSecurity` Interfaces. `core/interim.py`
resolves spanned HTML period/date/currency columns and supplies source facts
to the existing cash-flow reconciliation. It is an explicit January–June
reader, not a fallback from failed annual inline-XBRL parsing. Tax and balance
supplements remain core-owned; the Application compares source periods and
availability only after persisting the new judgment. See the
[research guide](tradingagents-research.md) for scope and commands.

Status is tracked in [`status.md`](status.md); this document describes only
how the current code works.

## Independent Quant research

`quant/` is a separate, standard-library research Module outside the core wheel.
Its `prepare_dataset` and `run_experiment` Interface centralizes point-in-time
market-only features, 20-session holding-return percentile labels, purged date splits
and evaluation. Historical market context uses each historical day's eligible pool;
model Adapters declare stock-only or stock+context and receive the same prepared
rows through `fit/predict`. Linear views include stock-by-market interactions. Research
signals carry explicit target/ranking and availability-time meanings. No Agent
route imports or consumes this module. Mechanism details and the offline
synthetic example are in [Quant contracts](../quant/CONTRACTS.md) and the
[Quant guide](../quant/README.md).

## TradingAgents research

The TradingAgents integration adds native-baseline and evidence-led research
commands behind the same Application Interface. The custom route uses
`FundamentalEvidenceTask` behind the existing core `run/replay`, followed by
isolated analysis/challenge drafts and fresh synthesis from audited evidence.
The synthesis receives required reference IDs, not the draft opinion text. Old-thesis
comparison happens after the new decision is saved. See
[tradingagents-research.md](tradingagents-research.md) for the execution paths,
optional integration environment and remaining limits.

## Cash-flow investigation task

The filing-based task runs through the same `handle/read_case` and `run/replay`
Interfaces. It reads source facts, calculates the earnings/cash-flow bridge,
chooses bounded note searches and saves a draft workpaper. It has no per-question
answer-profile input. See [cashflow-investigation.md](cashflow-investigation.md)
for the complete path, module map, source semantics and remaining limits.

The profile-based mechanism described below remains a separate task type;
its answer-key checks are not used by the cash-flow investigation.

## Two layers, two small Interfaces

```text
finresearchops CLI (thin)
      ↓
FinResearchOps Application            handle(command) -> ApplicationOutcome
  Case / Workpaper / Review / Packet   read_case(case_ref) -> CaseView
      ↓
FinAuditGate core                      run(AuditTask) -> AuditOutcome
  evidence + calculation admissibility replay(RunRef) -> ReplayReport
      ↓
CandidateModel.propose()               ScriptedModelAdapter | OllamaModelAdapter
```

Machine decisions (`ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW`) belong to the
core. Human actions (`APPROVE / RETURN / REJECT`) belong to the Application.
A machine `ACCEPT` always leaves a Case in `AWAITING_REVIEW`; only a person can
approve, and only an approved `ACCEPT` whose replay is consistent can be
exported. Change Packets are proposal-only.

## One run

1. **Normalize the task.** The task is serialized to canonical JSON and read
   back, so an Adapter cannot smuggle mutable state into what is executed.
2. **Choose the profile.** `SYNTHETIC_DEV` tasks are validated against the one
   public synthetic profile (`core/synthetic_profile.py`). `PRIVATE_DEV` tasks
   require a reviewed private profile (`core/profiles.py`) and a model trace.
3. **Ask for at most two proposals.** The Adapter is called with
   `attempt_index` 0 and, only if the first attempt was a recoverable `RETRY`,
   once more with 1. Every proposal is first converted into a bounded canonical
   snapshot (`core/proposal_snapshot.py`) so even garbage is replayable.
4. **Validate and calculate.** The profile executor checks the spans, hashes,
   values, normalized semantics, operand lineage, and formula, then computes
   the growth rate under a frozen `Decimal` context.
5. **Persist.** Eight artifacts (nine for traced runs) are written once under
   `runs/<run_id>/`; the run id is the SHA-256 of `identity.json`, which binds
   the task, candidate, attempts, model-trace summary, and policy hashes.

`replay()` reads the manifest, checks every artifact hash, rebuilds the
attempt sequence, re-runs the validation with no Adapter, and reports whether
the stored decision, answer, ledger, formula, and identity still follow.

## Persisted artifacts

Each artifact has exactly one schema version; see
[`../schemas/README.md`](../schemas/README.md) for the table.

## The local-model route

`adapters/ollama_route.py` is the single frozen definition of the route: model
tag and digest, system prompt, response schema, generation config, and budgets.
Their hashes are part of every trace. The trace and receipt field is still
named `tool_schema_sha256`: the route stopped offering the schema as a callable
tool and now sends it as the runtime's response `format`, and the field keeps
its name so no artifact needs a second schema version. It hashes exactly the
object sent as `format`, which is the object the model decodes against.

The response schema is closed (`adapters/ollama_contract.py`): fixed evidence
ids (`current`, `comparison`), enumerated metric, basis, unit, scale and sign,
a plain-decimal `value`, one `period` format, and `exact_span` as the number
as printed or the complete document line that carries it. The runtime decodes
against that schema, so the enumerations constrain generation as well as
decoding; the answer arrives as one JSON object in the message content and
`CANDIDATE_CONTENT_NOT_JSON` is the failure code when that content is not one
JSON value.

`exact_span` is located in the document bytes: first as a byte-exact copy, and
otherwise word by word with any run of whitespace between the words, because a
model transcribing a wrapped table row prints one space where the document
prints several or a line break. Only a single match is accepted, and the
offsets recorded are always the document's own, so the ledger keeps hashing
the bytes the document holds. The tolerant pass runs only when the exact bytes
appear nowhere, and both searches count placements the same overlap-aware way,
so tolerance can reach a region the exact bytes could not but can never
disambiguate a citation the exact search called ambiguous
(`EVIDENCE_SPAN_NOT_UNIQUE`). The proposal's identity is the canonical one,
rebuilt from the document's bytes, so a tolerantly located citation still
verifies offline; the model's own text stays bound by the response hash and
the raw bytes in the trace. The synthetic gate
resolves both the document's label and the model's claim through the same
alias registry and requires them to agree. The private gate compares the
claim's semantics and value with the reviewed profile literally, and accepts
the cited bytes only inside the reviewed line and only if they print the
value. In both cases the model, not the gate, has to name things correctly.

Per attempt the Adapter (`adapters/ollama.py`):

1. records the daemon version from `/api/version` (observed, never a gate);
2. requires that `/api/tags` lists the frozen tag with the frozen digest;
3. sends the frozen `/api/chat` request and captures the raw bytes with a
   fixed byte budget, keeping partial bodies;
4. derives one proposal or one failure code from those bytes
   (`adapters/ollama_trace.inspect_captured_response`);
5. writes one content-addressed trace under the private trace root and returns
   a receipt of hashes and codes.

`core/model_trace.verify_raw_model_trace` repeats steps 3–4 offline from the
saved bytes: it checks the trace hash, that the saved request equals the frozen
route request for this task and attempt, that the response bytes hash as
recorded, and that the same bytes still yield the same proposal or failure.
Raw request/response bytes never leave the private trace root; Workpapers bind
only the trace summary, and Packets carry no trace reference.

A shared daemon cannot prove which model bytes produced a response. The trace
therefore records the observed tag digest and daemon version; it does not
claim more.

## Private validation profiles

A profile is the reviewed answer key for one question on one frozen document:
exact byte spans, span hashes, values, normalized semantics, and the one
formula. Three shapes exist:

| Shape | Meaning | Gate outcome |
|---|---|---|
| two spans + formula | an acceptable answer exists | `ACCEPT` only on an exact match |
| `declared_published_at` after `task_cutoff` | the document is post-cutoff | `HUMAN_REVIEW / POST_CUTOFF_DOCUMENT` |
| empty `evidence_allowlist`, null `calculation` | nothing in the document answers the question | `HUMAN_REVIEW / NO_ADMISSIBLE_EVIDENCE` |

`scripts/build_validation_profile.py` builds a canonical profile from reviewed
facts by unique byte search.

## Application persistence

Case state is derived from an append-only event journal with an integrity
head. Material transitions (a new Workpaper, a new Review) are written as a
content-addressed transaction intent, published, then sealed with a commit
receipt; readers verify receipts independently, so an interrupted transition
is either invisible or exactly resumable by the same command. Per-Case POSIX
locks serialize local writers. This is deliberately heavier than a single-user
CLI needs and is a candidate for later simplification; it is fully tested.

## Boundaries

- Only `run-analysis` constructs a model Adapter. Everything else reopens the
  Application offline.
- `PRIVATE_DEV` documents, profiles, traces, artifacts, and paired outputs
  must resolve below the workspace's sibling `private/` tree; the artifact root
  fixes one workspace anchor and mixed workspaces are rejected.
- The core is standard-library only. The local runtime is an external
  application, not a Python dependency.

Research thesis requests distinguish substantive hypotheses and research constraints
from an explicitly recorded user view. The latter remains in the Case/report but
is excluded from the main model request projection. Free text is not automatically
rewritten or certified as neutral; see [the input contract](thesis-research.md).
