# Architecture

Status is tracked in [`status.md`](status.md); this document describes only
how the current code works.

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
