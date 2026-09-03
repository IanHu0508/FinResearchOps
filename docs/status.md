# Project Status

> Updated 2026-09-03. This file is the only public status source; other
> documents describe mechanisms and link here.

| Area | Status | Evidence |
|---|---|---|
| Deterministic core (`FinAuditGate.run` / `replay`) | Implemented for one public synthetic profile and for private validation profiles | 134 offline tests; `scripts/synthetic_demo.py` |
| Application (`FinResearchOps.handle` / `read_case`) | Implemented: Case, Workpaper, append-only Review, proposal-only Packet, replay records, crash recovery | `tests/test_application.py` |
| Local model Adapter (shared Ollama, Qwen3-4B) | Implemented: frozen request with a closed tool vocabulary and five fictional example rows, bounded raw capture, one content-addressed trace per call, offline verification | `tests/test_ollama_adapter.py`, `tests/test_model_trace_binding.py` (mocked exchanges); three route revisions and one model experiment on real text, 2026-09-03 |
| CLI `finresearchops` | Implemented: six thin actions over the Application Interface | `tests/test_cli.py`, installed wheel `--help` |
| Private validation profiles | Implemented: acceptable answer, post-cutoff document, no admissible evidence | `tests/test_private_dev_profile.py`, `scripts/build_validation_profile.py` |
| Paired evaluation runner | Framework only, no results | `tests/test_paired_runner.py` |
| Real issuer document (Tencent 2025 annual report) | Acquired locally under `private/`; text extracted; 12 candidate cases across six failure classes; human QA signed for all 12 (all INCLUDE); all 12 run through the full chain, replayed, and closed by the reviewer | private workspace only |
| Real-model results on real text | Twelve reviewed cases, thirteen runs, on the frozen route (v3): `ACCEPT` 7, `HUMAN_REVIEW` 2, `RETRY` 4; unsafe accepts 0; every accepted answer equals the reviewed answer; replay consistent 13 of 13. The earlier route (v1) gave `ACCEPT` 5 / `HUMAN_REVIEW` 5 / `RETRY` 2 with the reviewer's `APPROVE` 5 and `REJECT` 8 recorded; reviewer actions on the v3 runs pending. Three of the accepts are one evidence pair on different slices | private workspace only; counts below |
| Evaluation results | Paired ungated baselines exist (same model, no tools, no gate): the 4B model gave a wrong percentage on 11 of 11 answerable questions, the 8B model on 10 of 11, while the gate gave 0 wrong answers. Counted observations on one filing, not an evaluation | private workspace only |
| User interface | None | — |
| Git | Checkpoint commit on 2026-09-02 (parent `9c592f1`); route revision and these counts committed on 2026-09-03; no remote | `git log` |

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

## Twelve reviewed cases on real text (2026-09-03)

Each case is one reviewed slice of the Tencent 2025 annual report, one
question, and one validation profile built from facts the reviewer signed
off in the PDF. The local model (Qwen3-4B) is asked once, with one retry.

| Failure class | Case A | Case B |
|---|---|---|
| cutoff | `HUMAN_REVIEW / POST_CUTOFF_DOCUMENT` (day-level post-cutoff; the model fabricated a candidate from the cover text) | `ACCEPT` (cutoff after publication) |
| period | `ACCEPT` (income-statement flow) | `HUMAN_REVIEW / FISCAL_PERIOD_CONFLICT` (a balance-sheet date labelled as a fiscal year) |
| metric | `ACCEPT` (as-reported figure chosen next to a non-IFRS column) | `RETRY` (the total column cited for a segment question, plus labels outside the vocabulary) |
| unit | `HUMAN_REVIEW / METRIC_CONFLICT` (per-share figure labelled `other` / `PERCENT`) | `RETRY` (share count labelled with a scale word instead of `COUNT`) |
| missing evidence | `HUMAN_REVIEW / NO_ADMISSIBLE_EVIDENCE` (a year printed on the cover proposed as a value) | `ACCEPT` (sub-row chosen correctly next to its parent and total) |
| formula | `ACCEPT` on the reviewer's slice with column headers; `RETRY` on the same numbers without headers (digits invented, periods swapped) | `HUMAN_REVIEW / UNIT_CONFLICT` (per-share figure labelled `PERCENT`) |

What the counts say and do not say:

- no wrong answer passed the gate; every non-`ACCEPT` traces to a real model
  error, a label outside the closed vocabulary, or a cutoff refusal by design;
- the same two numbers were read correctly with three header lines in the
  slice and misread without them; context, not the route, made the difference;
- labels outside the vocabulary (a free-text metric, a currency or scale word
  in `unit`) are rejected before the gate and therefore counted as `RETRY`,
  which hides the financial error behind them; a later route revision should
  add segment-revenue and share-count metrics, say that `MILLION` is a scale
  and per-share figures are `PER_SHARE`, and define currency for non-monetary
  quantities. No such change is made mid-batch;
- the task cutoff is a date; a workpack cutoff one second before publication
  can only be represented as the previous day;
- how many of these the ungated model would have answered wrongly is the
  paired-baseline question and has not been measured.

## Route revisions and the model experiment (2026-09-03)

The batch above was run three more times on the same day, each time under a
changed route, every run replayed:

- v2, Qwen3-8B with a wider vocabulary and example rows: `ACCEPT` 1,
  `RETRY` 11. The larger model collapses whitespace inside cited spans (so the
  byte-exact copy fails), malforms the fixed calculation block, drops fields,
  and once changed a digit; calls took two to three times longer. Not adopted;
- v2 prompt on the 4B model: the per-share cases became `ACCEPT`, but the
  wording "never a currency code" led the model to write the currency as the
  unit on plain revenue rows. Not adopted;
- v3, 4B with positive-form descriptions and five fictional example rows:
  `ACCEPT` 7, `HUMAN_REVIEW` 2, `RETRY` 4, nothing lost against v1. Frozen.

Ungated baselines on the same thirteen tasks: the 4B model found the two
numbers in most cases and divided them wrongly every time (one sign error, one
inverted pair); the 8B model found the right numbers in every answerable case
and still divided wrongly ten times out of eleven. Model size bought evidence
selection, not arithmetic; the deterministic calculation step is what turns
found evidence into a right answer, at either size.

## Not proven

No number in this repository is an evaluation result. Twelve reviewed cases
on one real filing, judged by one reviewer, with one ungated baseline per
model, are counted observations. Nothing here should be described as a
completed agent, a benchmark, or a paper reproduction.
