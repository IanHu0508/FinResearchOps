# M2 Runtime Contract

> Status: `IMPLEMENTATION_COMPLETED / M2_GATE_PASS` in the current uncommitted,
> locally hash-frozen worktree. The renewed full-M2 Standards/Spec review,
> source/clean-wheel verification, and the top-level candidate-shape retry,
> Application failure-atomicity, and cross-Case ownership re-audits pass. This
> is the controlling M2 contract; closure evidence is tracked in the private
> planning index.

## Confirmed seams

FinAuditGate keeps its two-method external Interface:

```text
run(AuditTask) -> AuditOutcome
replay(RunRef) -> ReplayReport
```

FinResearchOps exposes one Application Module Interface:

```text
handle(command) -> ApplicationOutcome
read_case(case_ref) -> CaseView
```

The closed command set is `CREATE_CASE`, `RUN_ANALYSIS`, `SUBMIT_REVIEW`,
`EXPORT_CHANGE_PACKET`, and `REPLAY_RUN`. Inspection is `read_case`; it is not a
sixth command. Tests and callers cross these same seams. No CLI, UI, or test may
reimplement evidence, financial-semantic, formula, machine-gate, review, or
export rules.

## M2-only inputs

- Only original `SYNTHETIC_DEV` fixtures and `ScriptedModelAdapter` candidates
  are in scope.
- No model runtime, issuer filing, network, account, key, search, OCR, arbitrary
  code, database server, or UI is required or permitted in an M2 test path.
- A frozen document is immutable submitted bytes plus caller-declared metadata;
  M2 does not claim source authenticity.

## Deterministic gate

An `ACCEPT` requires one registered, content-bound profile and a complete chain:

```text
document SHA-256
-> byte locator and span SHA-256
-> normalized ledger node
-> ordered operand ids
-> allowlisted Decimal formula and frozen policy hash
-> answer and output unit
```

Fiscal-period and metric labels are resolved only through versioned registries.
Aliases may normalize to a canonical value; zero matches are unresolved and
multiple matches are ambiguous. Metric basis, currency, unit, scale, sign,
cutoff, operand identity/order, formula, output unit, quantization, and Decimal
context are deterministic policy inputs. The model proposes values but cannot
mark evidence verified, choose rounding, execute code, or select the final
decision.

### Decision ownership and stop rules

| Decision | M2 meaning | Retry inside the same `run()` |
|---|---|---|
| `ACCEPT` | All registered evidence and calculation checks passed | stop |
| `RETRY` | A recoverable candidate problem remained after the bounded budget | stop and preserve every attempt |
| `ABSTAIN` | The requested operation or evidence shape is outside the allowlist | stop immediately |
| `HUMAN_REVIEW` | A financial-semantic, cutoff, source-profile, or risk conflict requires a person | stop immediately |

The retry budget is exactly `1`: one initial proposal plus at most one retry.
Only recoverable candidate-shape, missing-evidence, locator, or claimed-value
failures consume that retry. A successful second candidate may become `ACCEPT`.
Exhaustion returns `RETRY` with both the specific failure and
`RETRY_BUDGET_EXHAUSTED`; it never calls the Adapter a third time. Unsupported
or arbitrary formulas return `ABSTAIN`. Period/metric/basis/currency/unit/scale/
sign conflicts or ambiguity, post-cutoff input, and material-risk restrictions
return `HUMAN_REVIEW`.

Before candidate validation, every proposal is converted into a bounded-output
canonical snapshot. Exact `ModelCandidate` fields, built-in containers, and
scalar values within the declared snapshot limits are preserved. Oversized
text, bytes, and integers receive a fixed-size descriptor that includes a
full-value SHA-256, so distinct scalar contents do not collapse merely because
their prefix, suffix, or length matches. Cyclic, over-depth, over-node,
over-item, or unsupported values receive a stable rejection descriptor and
cannot become a candidate. Full-value hashing is input-linear; the bound is on
the retained snapshot size and replay input, not on Adapter-supplied input
work. The proposal snapshot/hash, normalized candidate/hash (when one exists),
and disposition are all bound to the RunRef. Replay validates the stored
proposal-to-candidate transition under a recognized historical policy; it never
calls an Adapter, model, source, or network. JSON replay artifacts are read
with a byte cap before parsing and are rejected above the cap or nesting limit.

## Case lifecycle

The stable Case statuses are:

```text
CREATED
  -> RUN_ANALYSIS -> AWAITING_REVIEW
AWAITING_REVIEW
  -> APPROVE -> APPROVED   (machine decision must be ACCEPT)
  -> RETURN  -> RETURNED
  -> REJECT  -> REJECTED
RETURNED
  -> RUN_ANALYSIS -> AWAITING_REVIEW
```

`APPROVED` and `REJECTED` are terminal in M2. `ANALYSIS_RECORDED` is an atomic
persistence step, not a caller-observable resting state. Every machine decision,
including `ACCEPT`, leaves the Case in `AWAITING_REVIEW` until a human action is
recorded. A non-`ACCEPT` run may be returned or rejected but may not be approved.
Human review never changes or overwrites the primary AuditOutcome.

The journal head defines the committed visible event prefix. A durable pending
intent is not business authority and `read_case()` must never advance it. If a
transition was interrupted before the head changed, inspection returns the old
state; only a later public command with the exact canonical request identity may
finish that pending transition and must reuse its originally bound material.
A commit marker is only a receipt and is accepted only after material, event,
and committed journal prefix independently match. A head that advanced before a
receipt write is an uncertain-but-committed transition: inspection returns the
new state without duplicating the command. Only that current committed tail may
lack its receipt; an unsealed tail cannot coexist with a successor pending
intent, and any missing non-tail receipt fails closed.

Only the latest run of a Case may be reviewed. A returned Case may receive a new
run; earlier runs and reviews remain visible. Cross-Case, stale-run, duplicate,
unknown, terminal-state, and structurally similar fake references fail closed
without writing an artifact.

## Persistence and command repetition

- Case identity, frozen document bytes, Workpapers, Review Records, Change
  Packets, and successful Replay Reports are content-addressed or written once.
- Current Case state is derived from immutable artifacts; no Review Record or
  primary run is updated in place.
- Repeating an identical `CREATE_CASE`, eligible
  `EXPORT_CHANGE_PACKET`, or successful `REPLAY_RUN` is content-idempotent and
  returns the existing identity.
- A second analysis while review is pending, a second review, or a conflicting
  command is rejected rather than silently overwritten.
- Missing, malformed, unrecognized, or hash-inconsistent artifacts fail closed.
  An inconsistent core replay is returned as untrusted and is not persisted as
  a successful Application replay artifact.

## Workpaper and export eligibility

A Workpaper records the unique Case and RunRef, machine decision, answer shape,
document identity, gate reasons, and immutable ledger/formula/candidate/attempt
references. A `HUMAN_REVIEW` Workpaper must expose its reason and contain no
answer.

A Research Change Packet is eligible only when all of the following refer to the
same current Case and latest run:

1. the FinAuditGate decision is `ACCEPT`;
2. offline replay is consistent with that decision;
3. one append-only Review Record contains `APPROVE`;
4. the Workpaper, Review Record, ledger, formula, and RunRef identities agree.

Packet facts and calculations are copied from the replay-verified core artifacts,
not recalculated by FinResearchOps. Every Packet has `proposal_only=true` and no
field or behavior that changes a rating, valuation, portfolio, or formal research
state. `RETURNED`, `REJECTED`, non-`ACCEPT`, stale, ambiguous, or integrity-failed
Cases cannot export an approved Packet.

## Gate rule

Any unsafe `ACCEPT`, incomplete accepted lineage, machine-to-human state
conflation, overwritten primary/review artifact, exported ineligible Packet, or
different replay hard decision is `NO_GO`. Real-model, real-filing, evaluation,
CLI-product, and UI work are outside the current M2 candidate and were not
authorized or performed in this scope; they
require their own later Gate.

The frozen bounded scripted candidate passes this Gate. This closes M2 only;
M3 and every real-model/source/evaluation activity remain `NOT_STARTED` and
require separate authorization.
