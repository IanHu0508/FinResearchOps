# FinResearchOps / FinAuditGate Public Repository Rules

## Status

This is the single planned public repository for the two-layer project: FinResearchOps is the outward workflow and FinAuditGate is its trusted core. M1 is `COMPLETED` by the initial immutable commit, while the repository and core Implementation remain `PARTIAL`: only one exact content-bound synthetic `run()` / offline `replay()` profile, M1 core contracts, and product schema drafts exist. The FinResearchOps Application Module, generalized deterministic core, real Model Adapter, issuer inputs, experiments, and resume results are not complete. Never turn this narrow evidence into completed-Agent language.

## Public-release seam

This directory is the only public Git worktree. All files must be safe to publish. Private issuer material lives in the sibling `../private/` directory and must never be copied, symlinked, embedded, or referenced by a committed absolute path.

## Interface and Module rules

- Keep the package and repository identities `finauditgate` / `finaudit-gate`; do not create a second project for FinResearchOps.
- The planned FinResearchOps Application Module owns Case, human Review, Export, and replay coordination behind `handle(command)` and `read_case(case_ref)`. It is currently `NOT_STARTED`.
- Preserve the intended external Interface: `run(task)` and `replay(run_ref)`.
- Callers and tests cross the same Interface.
- Keep fiscal-period, metric, unit, evidence, calculation, retry, machine-gate, and core-artifact complexity inside the FinAuditGate Implementation. Keep Case lifecycle, human Review, Workpaper construction, and Packet export inside the FinResearchOps Application Implementation.
- Machine `ACCEPT` never means human `APPROVE`; the Application Module may not bypass a FinAuditGate hard failure.
- Add an Adapter Seam only when behavior actually varies. The planned model Seam has two Adapters: scripted and one local model.
- Models propose candidates; deterministic validators execute tools and decide admissibility.

## Dependency and data rules

- Python target: `>=3.12,<3.13`.
- Keep the deterministic core standard-library-only unless a demonstrated requirement justifies a dependency.
- Optional PDF/model dependencies stay inside their Adapters.
- Tests and CI use synthetic fixtures only and must not require network, models, accounts, or issuer data.
- No OCR, live search, arbitrary code execution, vector database, cloud service, or multi-Agent framework in the active MVP.
- UI remains `DEFERRED` until every v2 UI Entry Gate condition is satisfied, and must use the same Application Interface as CLI and tests.

## Verification and claims

- Use `unittest` for the initial offline suite.
- Every accepted number must eventually trace through document hash → locator → ledger → formula → answer.
- Replay never calls the model or network.
- Preserve raw results and negative cases; do not delete failed evaluation items.
- Do not report performance, test counts, deployment, reproduction, held-out, or business-impact claims before they are verified.

## Git safety

- No remote, commit, or push without explicit user authorization.
- Do not add a license until the user selects it.
- Before any public push, scan for private paths, secrets, PDFs, long source excerpts, logos, held-out gold, and raw model traces.
