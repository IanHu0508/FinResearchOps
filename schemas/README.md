# Schemas

Status: M1 schema-draft delivery `COMPLETED`; the FinResearchOps Application
Implementation and runtime cross-object enforcement remain `NOT_STARTED`.

Implemented FinAuditGate v1 contracts:

- `run-task-artifact.v1.schema.json`: the canonical persisted form derived from
  `AuditTask`; immutable document bytes are represented by their SHA-256.
- `run-ref.v1.schema.json`
- `audit-outcome.v1.schema.json`
- `replay-report.v1.schema.json`

`FrozenDocumentPackage` is the Python input value used by `AuditTask`. Its
`source_id`, name, and publication date are caller-declared metadata. Frozen
means the submitted bytes and declarations are immutable, not that the source
has been authenticated.

M1 FinResearchOps data-contract drafts:

- `case-record.draft.v1.schema.json`
- `workpaper.draft.v1.schema.json`
- `review-record.draft.v1.schema.json`
- `research-change-packet.draft.v1.schema.json`

The drafts use closed shapes, keep machine decisions separate from human
actions, and require proposal-only packets. The example under `examples/` maps
one real public-entry synthetic RunRef across the planned product artifacts.
JSON Schema alone does not prove that copied fields and artifact paths belong
to the same run, enforce append-only transitions, or authorize export; those
cross-object rules belong to the M2 Application Module.
