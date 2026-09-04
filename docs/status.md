# Project Status

> Updated 2026-09-03. This file is the only public status source; other
> documents describe mechanisms and link here.

| Area | Status | Evidence |
|---|---|---|
| Deterministic core (`FinAuditGate.run` / `replay`) | Implemented for one public synthetic profile and for private validation profiles | 148 offline tests; `scripts/synthetic_demo.py` |
| Application (`FinResearchOps.handle` / `read_case`) | Implemented: Case, Workpaper, append-only Review, proposal-only Packet, replay records, crash recovery | `tests/test_application.py` |
| Local model Adapter (shared Ollama, Qwen3-4B) | Implemented: frozen request with a closed response schema and five fictional example rows, schema-constrained decoding, whitespace-tolerant span location, bounded raw capture, one content-addressed trace per call, offline verification | `tests/test_ollama_adapter.py`, `tests/test_model_trace_binding.py` (mocked exchanges); four route revisions and two model experiments on real text, 2026-09-03 |
| CLI `finresearchops` | Implemented: six thin actions over the Application Interface | `tests/test_cli.py`, installed wheel `--help` |
| Private validation profiles | Implemented: acceptable answer, post-cutoff document, no admissible evidence | `tests/test_private_dev_profile.py`, `scripts/build_validation_profile.py` |
| Paired evaluation runner | Framework only, no results | `tests/test_paired_runner.py` |
| Real issuer document (Tencent 2025 annual report) | Acquired locally under `private/`; text extracted; 12 candidate cases across six failure classes; human QA signed for all 12 (all INCLUDE); all 12 run through the full chain, replayed, and closed by the reviewer | private workspace only |
| Real-model results on real text | Twelve reviewed cases, thirteen runs, on the frozen route (v3): `ACCEPT` 7, `HUMAN_REVIEW` 2, `RETRY` 4; unsafe accepts 0; every accepted answer equals the reviewed answer; replay consistent 13 of 13. A fourth route revision was run on both model sizes in three forms. As first specified it scored `ACCEPT` 4 / `HUMAN_REVIEW` 5 / `RETRY` 4 on the small model and `ACCEPT` 1 / `HUMAN_REVIEW` 3 / `RETRY` 9 on the larger one; with the schema restated in the prompt, `ACCEPT` 6 / `HUMAN_REVIEW` 2 / `RETRY` 5 and `ACCEPT` 6 / `HUMAN_REVIEW` 3 / `RETRY` 4; with one further worked example added to the numeric field's description, `ACCEPT` 7 / `HUMAN_REVIEW` 1 / `RETRY` 5 and `ACCEPT` 5 / `HUMAN_REVIEW` 4 / `RETRY` 4. Unsafe accepts 0 and replay consistent 13 of 13 in all six. The last of those three was tuned on the development cases and is marked as such below. The earlier route (v1) gave `ACCEPT` 5 / `HUMAN_REVIEW` 5 / `RETRY` 2 with the reviewer's `APPROVE` 5 and `REJECT` 8 recorded; reviewer actions on the v3 runs pending. Three of the accepts are one evidence pair on different slices | private workspace only; counts below |
| Evaluation results | Paired ungated baselines exist (same model, no tools, no gate): the 4B model gave a wrong percentage on 11 of 11 answerable questions, the 8B model on 10 of 11, while the gate gave 0 wrong answers. Counted observations on one filing, not an evaluation | private workspace only |
| User interface | None | — |
| Git | Checkpoint commit on 2026-09-02 (parent `9c592f1`); route revisions and these counts committed on 2026-09-03; no remote | `git log` |

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

## The fourth route revision (2026-09-03, not adopted)

Two changes were made to the contract, with the prompt wording otherwise frozen: a cited span is
located byte-exact first and only otherwise word by word with any run of whitespace between the
words (a second placement is still a refusal, and the recorded offsets are always the document's
own), and the schema is sent as the runtime's response format instead of as a callable tool, so
the answer arrives as one JSON object. One sentence of the system prompt changed with it.

Both changes did what they were meant to do, and the route still lost ground:

- the enumerated fields improved, because the runtime now decodes against the schema. A share-count
  case that three earlier revisions could not label correctly is finally labelled correctly by both
  model sizes;
- the free-text fields regressed. Switching the transport removes the schema's field descriptions
  from what the model reads — measured at 359 prompt tokens on the same task — and the two fields
  whose rules lived only in those descriptions started coming back wrong. Three cases that the
  third revision accepted now stop for human review;
- the larger model looked far worse under this transport, but that reading did not survive the
  next step (below).

Two defects in the first cut of the span change were found by an adversarial review of the diff
before any of these counts were taken, and both are fixed: the tolerant pass could resolve a
citation the exact search had called ambiguous, and a tolerantly located citation could never
verify against its own trace because its identity was computed from the model's text online and
from the document's bytes on replay. The offline suite grew from 134 to 144 tests, including the
end-to-end case that the second defect had made impossible.

Nothing here says schema-constrained decoding is wrong; it says the field descriptions have to
reach the model some other way. So the schema is now restated in the system message, in the same
bytes the route hashes, and the batch was run again on both model sizes.

That recovers two of the three lost cases and lets a no-evidence case reach its designed refusal
instead of a parse rejection. It also costs one case: shown the description for the numeric field,
whose two worked examples are both thousands-separator cases, the small model over-generalises and
strips the decimal point from a per-share figure. That defect is left standing — correcting the
example would be tuning the prompt on the twelve development cases, which is not allowed here.

One case still failed after that, and its cause was in a description too: the numeric field's two
worked examples were both thousands-separator removals, so the small model generalised them to
"remove punctuation" and dropped the decimal point from a per-share figure. A third worked example
was added in which the decimal point survives, using a fictional figure already present in the
prompt's example rows. That repaired the case, and the small model's accepted set is then exactly
the third revision's — nothing that passed before is lost.

**That last step was tuning on the development cases, at the user's explicit instruction, and its
counts are development-set counts.** The protocol bars tuning on evaluation items after a freeze;
these cases are the development set and nothing is frozen for evaluation, so it is permitted, but no
claim of generalisation rests on it. The counts are also fragile at this sample size: changing that
one example moved the larger model from six accepted to five, because a currency label flipped on
one slice while the same evidence pair kept its label on another. Temperature and seed are zero, so
that is sensitivity to prompt bytes, not sampling.

It also corrects the paragraph above. **The earlier finding that the larger model is clearly worse
was measured on routes that starved it of the field descriptions, and it does not survive.** Given
the same information as the small model it matches it, and it is the only model in any revision to
resolve the segment-versus-total case, citing the segment row rather than the group total. That is
not evidence it is better: the two sizes score the same on twelve development cases and simply fail
differently, and the larger one costs two to three times the latency. What it settles is that the
earlier refusal was an artefact of the contract, not a property of the model, and cannot be cited
against it. The size decision belongs to held-out text.

## Preparing the second filing (2026-09-03)

Nothing has been run on a second issuer. Before anything could be, two defects
in that path were found and fixed:

- the post-freeze split was declared in the contracts but not implemented. A run
  in that mode fell through to the public synthetic answer key: it required no
  reviewed profile, required no model trace, wrote the synthetic fixture as its
  policy artifact while the document was a real filing, refused every case with
  one misleading reason code, and *replayed consistently*. A batch would have
  passed every automated check this repository owns and meant nothing. The split
  now runs through the same checks as a development run, and the profile and the
  task must agree on which split they belong to;
- the private-storage guard was keyed to the development mode alone, so creating
  a case in the post-freeze mode would have written a second issuer's bytes
  outside the private tree. It is now keyed to both reviewed modes;
- separately, the frozen model digest was recorded in every trace but never
  checked offline, so "this run used the frozen weights" was a precondition the
  online Adapter enforced rather than something a saved trace proves. Offline
  verification now binds it.

None of this moves the route: prompt, response schema, generation config, model
identity and budgets are unchanged, and the development runs still replay.

The route surface is now frozen at commit `13685b4`, and a freeze declaration with
a pre-registered protocol and pre-registered predictions was written before any
second-issuer document was acquired or read. That declaration names the intended
issuer and case design, so it stays in the private workspace; its SHA-256 is
recorded here instead, which fixes its content and timestamps it without
publishing it:

    f8c9a1ebd6ae312eff3c4b472cf8232e7dc44ca1ec05dd2912ddb74e5f5cf375

A second filing has since been acquired as a frozen accession and extracted with a
deterministic, dependency-free extractor whose script hash is recorded with its
outputs. The sealed case pack — twelve cases across the same six failure classes,
each with its slice and its complete gold record — was built and hashed **before
the first model call**, and that hash is recorded here for the same reason:

    ea8200e94896a00186f4f49b618ebcbd47b4d321a9a9f0f866029288a1fb6e97

It has since been run on both model sizes, under the frozen route, in the
post-freeze split. On twelve cases across the same six failure classes: the small
model `ACCEPT` 3 / `HUMAN_REVIEW` 3 / `RETRY` 6, the larger model `ACCEPT` 6 /
`HUMAN_REVIEW` 3 / `RETRY` 3, **unsafe accepts 0 in both** — every accepted answer
equals the gold that was sealed before the first call, checked mechanically — and
replay consistent 12 of 12 in both. No human review disposition has been recorded,
so "no unsafe accept" here means "equals the sealed gold", not "a reviewer signed
it". These counts are kept apart from the development counts and must not be pooled
with them.

The pre-registration binds the run in advance: the case list and gold are hashed
before the first model call and never edited afterwards, a slice is never re-cut
after seeing a result, every case that starts is reported, both model sizes or
neither, development and transfer counts are never pooled, and predicted contract
refusals are declared ahead of time and counted apart from reading errors. Two of
those rules exist because the development set shows the failure they prevent, and
both instances are disclosed in the private record.

## What the second filing showed (2026-09-03)

The gate transferred. On a different issuer, a different filing format, a different
accounting framework and a harder table layout, no wrong answer passed: every wrong
column, fabricated span and mislabelled row became a refusal. Both designed refusal
branches — a document published after the task cutoff, and a slice carrying no
admissible evidence — behaved identically on both model sizes. Negative growth rates
were exercised end to end for the first time, since every development accept had been
positive.

Two things did not transfer, and both are recorded as findings rather than smoothed
over:

- the per-share vocabulary is genuinely inadequate for this filing. It prints two
  blocks whose row labels are character-identical, one per share and one per American
  Depositary Share, and the schema has no way to say which is meant. Three cases fail
  on both models for this reason. It is a contract defect to fix on the contract, not
  by tuning wording against these cases;
- column selection dominated the small model's failures. This filing puts three
  currency-of-report year columns plus a convenience-translation column on one row,
  where the development filing had two columns. That difficulty was **not** predicted
  in advance, and is logged as unpredicted.

One confound must be stated with any number from this run: the issuer changed and the
source format changed at the same time, from a PDF whose table rows wrap across lines
to HTML whose rows survive intact. Rows are easier to cite in the second, so improvement
cannot be credited to the contract alone.

## The vocabulary repair, and why it is not re-scored (2026-09-03)

The transfer evaluation found one defect that is the contract's, not the model's: a
filing may print an amount per ordinary share and the same amount per depositary
share in two blocks whose row labels are character-identical, and the schema had no
term for the difference. No reviewer could write an answer key the model could
reliably hit, and three cases failed on both model sizes for that reason alone.

The repair adds the missing term to the unit enumeration — `PER_DEPOSITARY_SHARE`
alongside `PER_SHARE` — and says in the field description to choose by the block
heading rather than the row label. The metric is unchanged; the unit now carries the
distinction, so citing the wrong block is a unit conflict the gate can state instead
of an ambiguity nobody could express.

**That repair changes the response schema, and the schema is restated in the prompt,
so it changes the route.** Three consequences are recorded here so they are not
quietly lost:

1. the transfer evaluation is **closed** against the route it was run on, and its
   counts belong to that route and that commit permanently;
2. the repaired route is **un-evaluated**. Nothing has been run on it beyond the
   offline suite;
3. re-running the sealed case pack on the repaired route would be changing the rules
   after seeing the evaluation and then scoring on the same items. It is not done, and
   any future number from those twelve cases would not be a transfer result. Testing
   this repair honestly needs a fresh sealed pack, on a filing that has not been used.

This is the intended use of an evaluation — it found a real defect — and the cost of
using it that way is that the next transfer claim needs new material.

## Not proven

No number in this repository is a benchmark result. Twelve reviewed cases on a
development filing and twelve pre-registered cases on a second filing, all
authored and judged by one person, with one ungated baseline per model, are
counted observations on two documents. The second run was pre-registered and
sealed before it was run, which makes it harder to fool yourself; it does not
make it independent, because the questions, the answer key and the review are
still the same person's.

What is deliberately not claimed:

- **this is not an agent yet.** It executes one fixed task shape. There is
  exactly one allowlisted operation, a year-on-year growth rate over exactly two
  evidence items; it does not decide what to check, retrieve its own material, or
  plan a sequence of steps;
- the document slice a run sees is still chosen by a person. There is no
  automatic slicing, so nothing here runs end to end on a filing unattended;
- roughly half the cases on a new filing need a human, and that is the design
  working, not a hidden cost that has been measured away;
- no reviewer disposition has been recorded for the second filing at all;
- the repaired route has not been evaluated on anything.
