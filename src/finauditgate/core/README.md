# Financial calculation and replay core

FinAuditGate provides the project's deterministic financial calculations and source checks. Its public execution interface is `run()` / `replay()` in `engine.py`.

| Responsibility | Implementation |
| --- | --- |
| Financial inputs and formulas | Cash-flow reconciliation, annual forward scenarios and parameter revisions in the task-specific core modules |
| Evidence and semantics | Source positions, period, currency, scale, attribution and operand lineage |
| Candidate snapshots | `proposal_snapshot.py` captures a bounded canonical proposal before validation |
| Profiles and aliases | `synthetic_profile.py`, `profiles.py` and `private_profile.py` define the retained reviewed-profile task |
| Trace verification | `model_trace.py` derives recorded candidates from captured requests and responses |
| Artifact storage | `artifacts.py` provides canonical serialization, hashes and append-once writes |

The Application coordinates Cases, reports and review actions. The core supplies reusable financial and provenance behavior behind that workflow. Internal helpers keep the public Interface stable.
