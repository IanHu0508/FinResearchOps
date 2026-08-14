# Core Implementation

Home of the FinAuditGate Module: `run()` / `replay()`, machine-gate validation,
evidence ledger, deterministic calculation, and artifact invariants. Case,
Workpaper, human Review, and Change Packet export are owned by the separate
FinResearchOps Application Module, not this core.

Current status is `PARTIAL`: two exact content-bound synthetic profiles,
frozen-byte locators, versioned M2 semantic registries, deterministic Decimal
lineage, a one-retry/two-attempt ceiling, four machine decisions, append-once
artifacts, and offline replay/v1 and replay/v2 exist. The registries are bounded
to the frozen profiles; universal parsing, source authenticity, real-source
behavior, and a real model remain unimplemented. Internal classes must not
expand the external Interface.
