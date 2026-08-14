# Schemas

Status: M1 core and historical draft delivery `COMPLETED`; the formal M2
synthetic Application schema set and its runtime cross-object enforcement pass
the renewed full-M2 Gate. M2 runtime contracts are `COMPLETED` for the bounded
scripted slice.

Implemented FinAuditGate v1 contracts:

- `run-task-artifact.v1.schema.json`: the canonical persisted form derived from
  `AuditTask`; immutable document bytes are represented by their SHA-256.
- `run-ref.v1.schema.json`
- `audit-outcome.v1.schema.json`
- `replay-report.v1.schema.json`

M2 core replay contract:

- `replay-report.v2.schema.json`: binds the canonical attempt sequence and the
  nine-artifact M2 replay generation while preserving replay/v1 for M1 runs.

`FrozenDocumentPackage` is the Python input value used by `AuditTask`. Its
`source_id`, name, and publication date are caller-declared metadata. Frozen
means the submitted bytes and declarations are immutable, not that the source
has been authenticated.

Formal M2 FinResearchOps runtime contracts:

- `case-journal-head.v1.schema.json`
- `case-transaction.v1.schema.json`
- `case-transaction-commit.v1.schema.json`
- `case-record.v1.schema.json`
- `workpaper.v1.schema.json`
- `review-record.v1.schema.json`
- `research-change-packet.v1.schema.json`
- `replay-record.v1.schema.json`

The Application validates these closed shapes and implements the M2 rules JSON
Schema cannot express alone. The completed M2 candidate treats the head as the
committed visible prefix, an intent as non-authoritative pending work, and a
commit marker as an independently verified receipt. Case/run ownership,
append-only transition recovery, and proposal eligibility pass their targeted
re-audits and the renewed full-M2 Gate.

Historical M1 FinResearchOps data-contract drafts:

- `case-record.draft.v1.schema.json`
- `workpaper.draft.v1.schema.json`
- `review-record.draft.v1.schema.json`
- `research-change-packet.draft.v1.schema.json`

The draft files and `examples/synthetic-product-journey.draft.v1.json` preserve
the M1 contract snapshot. Their embedded `NOT_STARTED` fields describe that M1
historical snapshot, not the current M2 runtime. They are not accepted as M2
runtime artifacts.
