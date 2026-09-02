# Project Status

> Updated 2026-09-02. This file is the only public status source; other
> documents describe mechanisms and link here.

| Area | Status | Evidence |
|---|---|---|
| Deterministic core (`FinAuditGate.run` / `replay`) | Implemented for one public synthetic profile and for private validation profiles | 131 offline tests; `scripts/synthetic_demo.py` |
| Application (`FinResearchOps.handle` / `read_case`) | Implemented: Case, Workpaper, append-only Review, proposal-only Packet, replay records, crash recovery | `tests/test_application.py` |
| Local model Adapter (shared Ollama, Qwen3-4B) | Implemented: frozen request, bounded raw capture, one content-addressed trace per call, offline verification | `tests/test_ollama_adapter.py`, `tests/test_model_trace_binding.py` (mocked exchanges) |
| CLI `finresearchops` | Implemented: six thin actions over the Application Interface | `tests/test_cli.py`, installed wheel `--help` |
| Private validation profiles | Implemented: acceptable answer, post-cutoff document, no admissible evidence | `tests/test_private_dev_profile.py`, `scripts/build_validation_profile.py` |
| Paired evaluation runner | Framework only, no results | `tests/test_paired_runner.py` |
| Real issuer document (Tencent 2025 annual report) | Acquired locally under `private/`; text extracted; 12 candidate cases prepared; human QA not run; no model run on real text | private workspace only |
| Real-model results on real text | None yet | — |
| Evaluation results | None | — |
| User interface | None | — |
| Git | Checkpoint commit made on 2026-09-02 (parent `9c592f1`); working tree clean; no remote | `git log` |

## What changed on 2026-09-02

The code was simplified without removing any user-visible capability:

- the experimental isolated llama.cpp route (macOS sandbox, model snapshots,
  Unix sockets) was archived;
- the three-level "assurance" taxonomy and the pre/post daemon identity checks
  were removed; a trace now records what was observed and the verifier checks
  that the saved request is exactly the frozen route;
- every persisted artifact has exactly one schema version; read-only
  compatibility branches for never-released formats were deleted;
- the retired M1 fixture and its code path were deleted;
- the Ollama daemon version is recorded in every trace but no longer gates a
  run (the desktop app auto-updates);
- two private-profile shapes were added so that post-cutoff and
  missing-evidence cases can be expressed;
- a Packet export bug for private-profile ledgers was fixed.

The pre-simplification tree is preserved outside the repository in
`../archive/2026-09-02-pre-simplification/`.

## Not proven

No number in this repository is an evaluation result. No `ACCEPT` has been
produced on a real issuer document. Nothing here should be described as a
completed agent, a benchmark, or a paper reproduction.
