# Core Implementation

Home of the FinAuditGate Module: `run()` / `replay()`, machine-gate validation, evidence ledger, deterministic calculation, and artifact invariants. Case, Workpaper, human Review, and Change Packet export are owned by the future FinResearchOps Application Module, not this core.

Current status is `PARTIAL`: an exact content-bound synthetic profile, frozen-byte locator, minimal ledger, one allowlisted Decimal formula, append-only artifacts, and offline replay exist. General resolvers, retry budgets, source authenticity, and real-source behavior remain unimplemented. Internal classes must not expand the external Interface.
