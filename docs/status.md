# Project Status

> Updated 2026-09-03. This file is the only public status source; other
> documents describe mechanisms and link here.

| Area | Status | Evidence |
|---|---|---|
| Deterministic core (`FinAuditGate.run` / `replay`) | Implemented for one public synthetic profile and for private validation profiles | 134 offline tests; `scripts/synthetic_demo.py` |
| Application (`FinResearchOps.handle` / `read_case`) | Implemented: Case, Workpaper, append-only Review, proposal-only Packet, replay records, crash recovery | `tests/test_application.py` |
| Local model Adapter (shared Ollama, Qwen3-4B) | Implemented: frozen request with a closed tool vocabulary, bounded raw capture, one content-addressed trace per call, offline verification | `tests/test_ollama_adapter.py`, `tests/test_model_trace_binding.py` (mocked exchanges); one live run on real text, 2026-09-03 |
| CLI `finresearchops` | Implemented: six thin actions over the Application Interface | `tests/test_cli.py`, installed wheel `--help` |
| Private validation profiles | Implemented: acceptable answer, post-cutoff document, no admissible evidence | `tests/test_private_dev_profile.py`, `scripts/build_validation_profile.py` |
| Paired evaluation runner | Framework only, no results | `tests/test_paired_runner.py` |
| Real issuer document (Tencent 2025 annual report) | Acquired locally under `private/`; text extracted; 12 candidate cases prepared; human QA signed for 1 of 12 (INCLUDE); that case run through the full chain | private workspace only |
| Real-model results on real text | One case (FY2025 vs FY2024 total revenues, one income-statement slice): `ACCEPT` on the first attempt, answer 13.86 %, offline replay consistent, `APPROVE` recorded by the reviewer, proposal-only Packet exported | private workspace only |
| Evaluation results | None; one case is an observation, not an evaluation | — |
| User interface | None | — |
| Git | Checkpoint commit on 2026-09-02 (parent `9c592f1`); route revision committed on 2026-09-03; no remote | `git log` |

## What changed on 2026-09-03

The first run on real issuer text showed the model reading the right row, the
right values, currency and periods, while the gate rejected the proposal for
reasons the route had never stated: free-text evidence ids and semantics, a
thousands separator in the value, and a whole-row span checked against a
number-only answer key. The route was revised so that the contract says what
the gate checks:

- the tool schema is closed (`adapters/ollama_contract.py`): evidence ids are
  `current` and `comparison`; metric, basis, unit, scale and sign are
  enumerations; `value` is a plain decimal string; `period` is `FYyyyy` or
  `yyyy-mm-dd`; `exact_span` is the number as printed or the complete line
  that carries it. The system prompt states the same rules;
- the private gate accepts a cited span only inside the reviewed line and only
  if it prints the reviewed value; everything else about the claim is still
  compared literally with the profile. Each ledger node now hashes the bytes
  the model actually cited and records the reviewed region separately, so a
  Packet's `span_sha256` names the cited bytes;
- the synthetic gate resolves the document's label and the model's claim
  through the same alias registry and requires them to agree;
- `scripts/build_validation_profile.py` takes `--current-line` /
  `--comparison-line` (a unique substring of the line) and validates the
  enumerations.

Route hashes changed twice on the way; every run stays under its own route
prefix in the private workspace. On the one reviewed case the three revisions
ended `RETRY / EVIDENCE_PROFILE_CONFLICT`, `RETRY / EVIDENCE_LOCATOR_INVALID`
(a number cited against a whole-row profile), and `ACCEPT` with a consistent
replay of 10 artifacts. The first `ACCEPT` run had been produced before the
ledger-node fix; under the corrected code its replay is refused
(`WORKPAPER_CORE_MISMATCH`), which is the intended behaviour when the core
changes, and the case was re-run, replayed, approved and exported on the
corrected core. Each model call took about 8–10 seconds on the local daemon.

A live run of the public synthetic fixture under the new route ended
`RETRY / MODEL_CANDIDATE_REJECTED`: the model echoed the fixture's alias labels
instead of the enumerations and wrote `150.00` as `15000`, and the decoder
rejected the call before the core saw it. The scripted synthetic demo is
unaffected; the live synthetic route with this 4B model is not a reliable
demonstration and is not claimed as one.

The 2026-09-02 simplification (archived isolated route, one schema version per
artifact, observed-not-gated daemon version) is described in the previous
revision of this file and in `docs/architecture.md`.

## Not proven

No number in this repository is an evaluation result. One `ACCEPT` on one
reviewed slice of one real filing, approved by one reviewer, is a single
observation. Nothing here should be described as a completed agent, a
benchmark, or a paper reproduction.
