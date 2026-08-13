# FinResearchOps / FinAuditGate Architecture Contract

> Status: `PARTIAL`. The FinResearchOps product workflow is frozen at planning level. The FinAuditGate two-method Interface and one synthetic vertical slice are implemented; the Application Module and full flow below remain planned.

## Two-layer ownership

```text
CLI / later UI
      ↓
FinResearchOps Application Module
  Case / Review / Export / replay coordination
  handle(command) / read_case(case_ref)                 NOT_STARTED
      ↓
FinAuditGate
  evidence and calculation admissibility
  run(task) / replay(run_ref)                           PARTIAL
      ↓
Model Adapter / Source Adapter / Artifact Store
```

Machine `ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW` belongs to FinAuditGate. Human `APPROVE / RETURN / REJECT` belongs to FinResearchOps. Neither layer may reinterpret the other layer's hard failure, and the Change Packet remains proposal-only.

## FinAuditGate external Interface

```text
run(AuditTask) -> AuditOutcome
replay(RunRef) -> ReplayReport
```

The implemented Interface includes fail-closed validation, immutable task/candidate snapshots, versioned profile binding, deterministic artifact identity, append-only writes, and the rule that replay performs no network or model calls. Bounded retry and generalized period/metric/unit resolution remain M2 work.

## Implemented M1 core flow

```text
normalize AuditTask and immutable submitted bytes
→ snapshot the untrusted scripted candidate
→ enforce the exact content-bound synthetic profile
→ build Evidence Ledger
→ deterministic Decimal calculation
→ append-only artifact commit
→ return ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW
→ replay by stored recognized policy identity
```

The model never verifies evidence or commits state. It supplies candidates to deterministic validation and execution. The current scripted slice compares candidate semantics with frozen byte spans and requires the exact registered synthetic source id and document hash before an allowlisted Decimal calculation can be accepted.

Workpaper construction, human Review Records, Case transitions, and Change Packet export belong to the future FinResearchOps Application Module. They are deliberately absent from the core Implementation. M1 contains only their closed schema drafts and one non-executable mapping example.

## Seams

- Model candidate Seam: scripted and local-model Adapters.
- Source acquisition and authenticity: kept outside the current core. `FrozenDocumentPackage` freezes submitted bytes plus declared metadata but does not verify an official locator or publication date; trusted acquisition manifests are later work.
- Artifact persistence: local content-addressed filesystem first; no database server.

The planned Application Module is a business caller of FinAuditGate, not a second repository or package. CLI, tests, and any later UI must cross its same Interface. UI remains deferred until the v2 UI Entry Gate is satisfied.
