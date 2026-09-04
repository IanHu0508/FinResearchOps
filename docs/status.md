# Project Status

> Updated 2026-09-03. This file is the only public status source; other
> documents describe mechanisms and link here.

| Area | Status | Evidence |
|---|---|---|
| Deterministic core (`FinAuditGate.run` / `replay`) | Implemented for one public synthetic profile and for private validation profiles | 172 offline tests; `scripts/synthetic_demo.py` |
| Application (`FinResearchOps.handle` / `read_case`) | Implemented: Case, Workpaper, append-only Review, proposal-only Packet, replay records, crash recovery | `tests/test_application.py` |
| Local model Adapter (shared Ollama, Qwen3-8B) | Implemented: frozen request with a closed response schema and five fictional example rows, schema-constrained decoding, whitespace-tolerant span location, bounded raw capture, one content-addressed trace per call, offline verification | `tests/test_ollama_adapter.py`, `tests/test_model_trace_binding.py` (mocked exchanges); four route revisions and two model experiments on real text, 2026-09-03 |
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

None of this moved the route at the time: prompt, response schema, generation
config, model identity and budgets were unchanged, and the development runs still
replayed. That sentence was true when written and is no longer true of the current
code — see the note on what "replays" means, below.

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
replay consistent 12 of 12 in both **at the time they were run, against the route
they were run on**. No human review disposition has been recorded, so "no unsafe
accept" here means "equals the sealed gold", not "a reviewer signed it". These
counts are kept apart from the development counts and must not be pooled with them.

### What "replays" means here, precisely

Offline verification checks a saved trace against **the route the code currently
ships**, not against the route the run was made on. So changing the route — as the
depositary-share repair did — makes every earlier run stop verifying under the new
code, reporting a core mismatch. That is the intended fail-closed behaviour and not
a corruption: the artifacts are intact, and each batch still verifies under the
commit that produced it.

It does mean a replay claim is only ever true of a (run, commit) pair. Every "replay
consistent" count on this page was established at run time against that batch's own
route, and none of them can be reproduced at the current HEAD. Anyone re-checking
them must check out the commit named for that batch.

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

## Corroborating a figure against its second printing (2026-09-03)

A filing prints the same figure more than once: a total in the statement and
again in the note that breaks it down. Every figure reviewed so far on the two
filings used here is printed between four and ten times.

A validation profile can now record where a figure is printed again, and the
gate reads that second region from the frozen document and requires it to carry
the same value. If it does not, the run stops for a person
(`CORROBORATION_CONFLICT`); if it does, the agreement is recorded in the evidence
ledger alongside the citation. The check is computed from the document alone —
the model is never told a second region exists and cannot influence it — which
keeps it on the deciding side of the line rather than the proposing side.

The reviewer still chooses where the second printing is, so this corroborates a
citation; it does not go looking for contradictions on its own. The profile
schema moved to accommodate it, which makes profiles written for the previous
version unloadable; profiles are rebuilt for every run and replay does not read
them, so nothing recorded depends on the old shape.

## A second transfer evaluation, pre-registered (2026-09-03)

Repairing the defect the first transfer evaluation found changed the route, so the
repaired contract has been evaluated on nothing. Three claims are unsupported and a
second run on a third issuer exists to test them: that the depositary-share repair
works; that the slicing rules generalise rather than fitting the two documents they
were written against; and that the transfer result is a property of the contract
rather than of one issuer. Two capabilities have also never run on real text — the
second operation, and the corroboration check.

The route is frozen at commit `e8051fe`, and a freeze declaration with the
pre-registered protocol and predictions was written before any third-issuer document
was acquired or read. It names the intended issuer, so it stays private; its SHA-256
is recorded here instead, which fixes its content and timestamps it:

    f0570a4bbfe7036cd3d18a51a0dd9f0d2f537d8fcf2fdc54f516caa693fec4e9

The binding rules are the same as the first run, with one addition: slices are cut by
the slicing rules rather than by hand, and where several candidate regions match, the
choice is recorded before the run. Hand-editing a produced slice is a deviation and
is logged as one.

A third filing has since been acquired as a frozen accession, from an issuer whose
filing is prepared by a different filer agent than the second, and extracted with
the same extractor unchanged. The sealed case pack — twelve cases across the same
six failure classes, with slices cut by the slicing rules rather than by hand — was
built and hashed before the first model call:

    85ab21a5ccc4be339699a9e81895573677e0a17dcf6f714f877ed5d327638ff5

Two things are recorded in it before any model ran. The slicing prediction is
already partly falsified: on unseen material the rules produced a sufficient slice
for eight of nine evidence-bearing cases, missing one because this filing heads its
balance sheet with a bare date where the previous one wrote "as of". That rule is
deliberately **not** being changed now — changing preparation after seeing the
evaluation material is what the pre-registration exists to prevent — so the case
carries a recorded prediction that it will refuse. Separately, one row could not be
separated from its narrative repeat by any section string, so the reviewer chose
among the slicer's own candidates, which the protocol permits and which is recorded
in the pack.

It has since been run on both model sizes. On twelve cases the small model accepted
none and the larger accepted two, **unsafe accepts 0 in both** — both accepts equal
the sealed gold — with replay consistent 10 of 10 in each arm. Two cases never
reached a model at all because of errors in how the author wrote their locators;
they are reported as errors rather than dropped, and the sealed pack was not edited
to repair them.

Verdicts on the three claims this run existed to test:

- **the depositary-share repair is supported, thinly.** The per-ordinary-share case
  was accepted with the right answer by the larger model, where on the previous
  filing every case of that shape failed on both models. Its per-ADS twin failed for
  a locator reason rather than a labelling one. One accept is evidence, not a
  demonstration;
- **the slicing prediction is partly falsified**, exactly as recorded before the run:
  eight of nine on unseen material, missing one because this filing heads its balance
  sheet with a bare date. The rule was not changed afterwards;
- **the transfer conclusion is weakened rather than confirmed.** The gate held again —
  no wrong answer passed on a third issuer in a third document shape — but accepts
  fell, and the cause was not predicted.

The dominant failure is one this project has now seen twice. This filing reports in
thousands where both earlier ones reported in millions; the model reads the figures
correctly and then labels the scale wrongly, because the scale description's worked
examples are all of one case. The same shape of defect — a description whose examples
cover a single situation, over-generalised by the model — was found earlier in the
numeric field. That is a pattern rather than two coincidences, and it is the next
thing to fix.

One nuance for reading any of these counts: a growth rate is scale-invariant, so in at
least one case the gate refused a claim whose derived percentage would have been
correct. The refusal is right, because the gate checks claims and not only answers,
but the accept count understates how often the arithmetic would have landed. No prediction is registered about how many cases are
accepted, because the author also chooses the cases, and a count target would invite
choosing easy ones.

## Two repairs after the second transfer run, and one idea abandoned (2026-09-03)

**A live defect, found while sizing a different change.** The generation context has to
hold the prompt and the answer together, and overflowing it is not an error: the runtime
discards the front of the prompt — where the system message and the schema are — and
answers normally. A run built that way completes, replays, and means nothing. This was
confirmed by direct probe, not inferred. Nothing guarded it, and the document budget
permitted a slice several times larger than the context could hold. The second transfer
evaluation ran 46 tokens below the ceiling; its prompts did fit, so its results stand,
but that was luck. The three constants are now sized so overflow cannot happen, and the
arithmetic relating them is pinned by a test — which immediately caught an error in the
first version of the sizing.

**One description repaired, on evidence.** Counting every conflict the gate has ever
raised, across both filings and the development set: currency 24, period 10, scale 5,
metric 3, unit 1, and **zero** for sign and basis. The scale description named one scale
with worked examples and the others in passing, and the third filing reports in a scale
the earlier two did not use. It now says that the scale differs between filings and must
be read from the table's own heading each time.

**And an idea abandoned, which matters more than the repair.** The plan had been a general
rule — every enumerated value gets a worked example — applied to four fields. The
repository refutes it. The `unit` description already exemplifies all five of its values
and still misfires; the numeric field carries three worked examples, added as an earlier
fix that appeared to work, and on the third filing the model still returned figures with
thousands separators intact. Adding worked examples is therefore a weak instrument with a
recorded non-effect, and two of the four proposed edits targeted fields that have never
produced a single conflict. A future failure must not be answered reflexively with more
prose.

Both changes move the route, so the second transfer evaluation is closed against the route
it ran on, and the repaired route has been evaluated on nothing. Whether the scale change
helps is a prediction to be registered before a fourth filing, not a claim to be made now.

## A third transfer evaluation, pre-registered (2026-09-04)

Two repairs after the second transfer run — the scale description and the generation
budgets — have been evaluated on nothing, and a third repair to the slicing rules was
made between evaluations and disclosed. The route is frozen at commit `2ebc68b` and a
declaration with the protocol and predictions was written before any fourth-issuer
document was acquired or read. It names the intended issuer and case design, so it stays
private; its SHA-256 is recorded here:

    9e6767f4fe25cdc181b04b169e9199091541293ad3c76e78b2fcfba24bc58339

The selection criterion is declared in advance and is deliberately targeted: the filing
must report at thousands scale, because the scale repair cannot be tested on a filing that
reports in millions. Choosing material that exercises a repair is legitimate; choosing it
afterwards would not be. If the acquired filing turns out not to report at that scale, the
prediction is untested and the report has to say so.

Two rules are added to the protocol from the previous run's mistakes: a sealed pack must
be provably buildable before it is sealed, because two cases last time never reached a
model owing to locators the author wrote badly; and the maximum prompt and answer of every
batch is reported against the context, so the budget repair is checked rather than assumed.

A fourth filing has since been acquired, chosen against the declared criterion and
verified to meet it: it reports at thousands scale, which is what makes the scale repair
testable. Its sealed pack — twelve cases, slices cut by the slicing rules, every locator
resolved at seal time — was built and hashed before the first model call:

    d47b324349d86b5c6dc98a03294c1e4a673262cc287fdc574db532f37a1e86d1

The new rule earned itself immediately: seven of the twelve cases could not be sealed on
the first attempt, and were repaired before sealing rather than discovered at run time as
they were last time. Two further things are recorded in the pack before any model ran.
The depositary-share repair cannot be tested here, because this filing's statements carry
per-ordinary-share figures only. And the bare-date slicing repair cannot be tested either,
because this filing uses the worded form the rules already handled — so that prediction is
marked untested rather than counted as passed.

No corroboration case is included, for a reason worth stating: the figures a filing prints
twice sit in byte-identical rows, and a locator that names a row by its text cannot
separate them. Forcing a pair would have been contrived, so corroboration remains without
a real-text result and the limitation is named instead.

It has since been run on both model sizes: the small model accepted none and the larger
two, both equal to sealed gold, **unsafe accepts 0**, replay 12 of 12 in each arm. The
budget repair is confirmed — the largest prompt and answer came to 3,338 tokens against a
context of 8,192, where the previous run peaked at 4,050 against 4,096.

**And the run overturned the repair it was built to test.** Every scale conflict in this
and the previous evaluation was caused by the slicing rules, not by the model and not by
the field description. A statement names its scale once, in a units line, and where that
line sits relative to the period header differs by filing. The rules began a slice at the
period header, so a filing that prints its units line above the header lost it. Counting
every sealed slice: nine of ten stated a scale on the first transfer filing, and **none of
ten on either of the other two**. In two of three evaluations no slice told the model what
scale the figures were in, so every scale label those models produced was a guess the gate
correctly refused. The first filing escaped only because it prints its units line below
the header, which is luck rather than a working rule.

Three things follow, and they are recorded rather than smoothed over. The description
repair was made on real evidence — five recorded conflicts — but the evidence did not
identify the cause, and the input was never checked for the information before the wording
was changed. The central prediction was therefore not falsified but **untestable as
constructed**, which is worse than being wrong. And the sufficiency check that should have
caught it asked for a period header and *currency or scale*, so a slice carrying a currency
and no scale passed; an "or" between two things that are not interchangeable is not a
check.

The slicing rules now reach up to the units line, with a page boundary preventing a
previous statement's units from being captured. That repair is un-evaluated in its turn.

The central prediction was registered at no better than even odds. The same instrument —
adding a worked example to a field description — has one recorded non-effect: an earlier
fix appeared to work on the set it was written against and did not hold on new material.
Whether the scale repair is any different is exactly what this run is for, and the
prediction that it may fail is written down in advance rather than after.

## Checking the input as strictly as the claim (2026-09-04)

Twice now a run completed, replayed, and meant nothing because the input was
defective rather than the model: once when a declared-but-unimplemented mode fell
through to the public answer key, and once when slices omitted the scale the claim
would be judged on. Both looked like model failures in the counts.

The preparation side now carries a check with the same shape as the gate's. A slice
is audited against what the reviewed claim actually depends on — the period, the
currency unless the claim declares none, the scale unless the amount is per-share —
and each requirement is reported separately rather than being satisfied by any one
of them. The `or` in the previous version is what hid the defect, so the test suite
pins that specific case: a slice naming a currency and no scale must be reported as
missing a scale.

Applied to the sealed packs already run, the check finds that **fifteen of thirty
evidence-bearing cases across three filings were unanswerable as constructed** — the
model was asked to name something the document it was shown did not state. One of
those is a deliberate contrast case that was always meant to lack its headers; the
rest were not deliberate. Re-cut with the repaired slicing rules, the affected cases
are complete.

This does not retract any published count. No wrong answer passed in any of those
runs, and refusals stay refusals. What it changes is the reading: a large share of
them were refusals of questions that could not have been answered from the material
given, which is a property of the harness and not of the model.

## The route model is now the larger one (2026-09-04)

Four batches have been run on both sizes, and the pattern is not in the totals but in
where each one wins. On the development filing — the set the prompt wording was tuned
against — the small model accepts more. On all three filings it had never seen, the
larger model accepts more, every time:

| batch | small | large |
|---|---|---|
| development filing | **7** | 5 |
| first transfer | 3 | **6** |
| second transfer | 0 | **2** |
| third transfer | 0 | **2** |

The small model's advantage exists only where the prompt was fitted to the cases. That
is the more useful reading of an earlier finding: a claim that the larger model was
worse came from routes that starved it of information, was retracted, and is now
inverted on unseen material.

The route is therefore the larger model. Its manifest records the digest and size the
daemon reports; the weights hash and licence hash are null rather than guessed, because
they were not independently obtained for this tag. The caveat stands unchanged: the tag
and its reported digest are frozen, the weights are not independently attested.

## Two capabilities finally exercised on real text (2026-09-04)

The corroboration check and the second operation had been carried through four filings
without a single real-text result. Both now have one, on the development filing, with the
larger model on the repaired route.

**Corroboration.** The reason it had never run was diagnosed rather than worked around. A
figure worth corroborating is printed either far outside any slice, or as an unlabelled
repeat of the same numbers — a total restated under its own breakdown — whose line carries
no text that distinguishes it from the first printing. Both defeat a locator that names a
row by its text, which is what the profile builder offered. The stored profile format was
never the problem: it holds a byte range. The builder now also accepts *which printing* a
corroborating figure is, and that resolves to the same byte range the gate already checked.

On the development filing this reaches the intended shape exactly: a profit line, its
attribution breakdown, and the breakdown's total restated beneath it. The honest pairing is
accepted and recorded in the evidence ledger as agreeing; a corroboration pointed instead
at the neighbouring equity-holders line — a real line carrying a different figure, with its
hash recomputed so only the value is wrong — is refused `CORROBORATION_CONFLICT`.

**The second operation.** `absolute_change` returns 33,334 on the same pair, in the unit of
the figures rather than in percent, replayed consistently. Every accepted answer before
this one was a percentage.

**Qualified on 2026-09-04, after counting something that should have been counted first.**
Every one of the six `absolute_change` cases run to date asks its question in a form that
names the scale — "by how much did X change, in RMB millions?" — and none of the thirteen
percentage cases does. That is not carelessness in six places. A difference is a raw
number, so the question has to say what unit the answer is wanted in; a percentage is
unitless and never needs it. The asymmetry is a property of the two operations.

The consequence is specific and it narrows a claim made above: the second operation has
been accepted with the correct value every time, but **never once on a question that left
the model to read the scale from the document.** Its acceptances are evidence that the
operation and its output unit work. They are not evidence about reading a filing's scale,
and the scale-labelling counts elsewhere in this document exclude them.

A five-case development pack exercising both, plus the repaired slicer, accepted
5 of 5 on the first attempt with every answer equal to the reviewed one and all
five replays consistent. On a development set that count is a functional check,
not an evaluation result.

Neither result says anything about a second issuer. They were produced on the development
filing, which is where iteration belongs, and they demonstrate that the two features work
at all — which until now was untested outside the offline suite.

## The slicer repair, measured against a control (2026-09-04)

A repair had been made to the way a slice is cut — the start now extends upward to
the line where a statement says what units its figures are in — and it had never
been run. Two earlier evaluations were re-cut with it and re-run, changing nothing
else: same questions, same reviewed answers, same prompt, schema, generation
config and model. A third arm ran the *original* slices on the current route, to
separate the route from the slice; it reproduced the earlier result on all ten
cases, so the comparison rests on the slice alone.

Measured offline, with no model: slices that state everything their reviewed claim
is judged on went from 6 of 20 to 17 of 20. Every one of the eleven repairs is a
restored scale.

Measured on the model, the honest number is smaller than the decisions suggest.
Two of the questions name the scale themselves and cannot test anything. One case
stopped reporting a scale conflict only because it now fails an earlier check
while still claiming the wrong scale. On the four cases where the question is
silent and the units line is present, the model got the scale right twice and
wrong twice. **The units line is necessary and not sufficient**: before the repair
the answer could only be a guess, and afterwards it is right about half the time
on these two filings. Reporting the decisions alone would have made a partial
result look like a solved problem, which is the same mistake an earlier
wording-level repair already made once.

Across the three arms — 32 runs — every accepted answer equals the reviewed one,
nothing wrong was accepted, and all 32 replays are consistent offline. Both
wrong-scale claims were refused. Accepted counts rose from 2 to 3 on each of the
two filings, and one previously accepted case regressed to a refusal; that cost is
recorded rather than netted away.

These filings were run, analysed, and only then repaired, so this measures whether
the repair does what it claims — not whether it generalizes. That still needs a
filing nobody has looked at.

## A fifth filing, declared before it is acquired (2026-09-04)

Three things have never been measured on a document nobody has looked at: the
slicing repair above, the input-sufficiency check, and the corroboration check.
`absolute_change` is in better shape — accepted on the development filing and on
both re-cut arms, always at the reviewed value — but likewise never on a filing
acquired after a freeze. A fourth transfer evaluation is declared for that.

The declaration names the intended issuer and case design, so it stays private;
its SHA-256 is recorded here, before acquisition:

    4d093ddda02beb5137acceaffc3c8d8bc693fa30a2542ccd299ee8b65410db26

That declaration set the criterion and left the issuer to be named. The issuer and
the frozen accession are named in an addendum, appended rather than edited in so
the hash above still stands, and its own hash is recorded before any byte of the
filing was fetched:

    af344a0cd2ff98fd08194212bc33f32fab3fdbca1ff02d8a73bc20f8659000d9

Freezing the accession before fetching is what stops a filing being chosen after
its contents are known. Two requests preceded it and neither read the filing: a
company lookup and the submissions metadata.

The freeze now also pins three files that are *preparation* rather than route —
the slicer, the operations table, and the profile builder. They change what the
model is shown without changing the route hash, which is precisely how the
slicing repair slipped between two evaluations unmeasured. Pinning them makes the
next such change visible instead of silent.

Two rules are added from this session's own mistakes. **A prediction about what a
model writes has to be written against the field the model writes, not against the
gate's reason code** — the slicing A/B was pre-registered against reason codes,
scored four of five, and the honest count against the claimed field was two of
four; the error flattered the repair. And **a flag with no test has not been run**:
one locator flag raised an exception on every call it had ever received, and
survived because nothing tested that script at all.

The selection criterion is declared in advance and excludes mainland-listed
issuers, on a measurement rather than a preference: against representative
Chinese-language statement text, all four of the slicer's period, units, scale and
currency patterns fail to match, and every case would be refused before a model
was called. A Chinese filing is not a harder instance of this evaluation; it is a
different system, and it belongs on its own route so these evaluations stay valid.

## A pre-seal check, built out of every defect that got past one (2026-09-04)

Three evaluations each surfaced a defect only after the run: cases whose slice could not
answer their own question, cases whose locator was the bare printed figure and so failed to
resolve when that figure appeared twice, and questions that named a field the run was
measuring. Each was found by hand, late, and after the pack was sealed.

They are now one check that runs before a pack may be sealed, and it fails the pack rather
than warning. Run against the three historical packs it flags, retroactively, every defect
that took three evaluations to find — including the two cases that never reached a model at
all, and all three questions that named the scale. Run against the current development pack
it flagged four more, in material written days ago.

The slicing repair that motivated it also got its second correction. Extending a slice
upward to its units caption was capped at six lines, because the two filings measured at the
time printed it within a few lines of the heading; a third prints it thirteen lines up, on
the same page, and those cases stayed unanswerable after the repair meant to fix exactly
them. The walk now runs to the page boundary, stopping at a second period header so it
cannot take the caption of the table above. Slices stating everything their reviewed claim
is judged on: **6 of 20 before the repair, 17 of 20 after the first version, 20 of 20 now.**

Two observations were twice mistaken for a rule here — first the six-line cap, then a guard
that counted a heading's year row as a second statement and stopped one line short of the
caption. Both were found by measuring rather than by reasoning about the code.

## Not proven

No number in this repository is a benchmark result. Twelve reviewed cases on a
development filing and twelve pre-registered cases on a second filing, all
authored and judged by one person, with one ungated baseline per model, are
counted observations on two documents. The second run was pre-registered and
sealed before it was run, which makes it harder to fool yourself; it does not
make it independent, because the questions, the answer key and the review are
still the same person's.

What is deliberately not claimed:

- **it covers very little of the benchmark this project was framed against.** On a
  ten-question development slice of that benchmark's public subset, none fits this
  system's task shape, and counting the graders' own rubric points rather than questions,
  16 % of the available credit is for finding a figure and 84 % is for deriving one. This
  system verifies the finding. Calling it a reproduction would be wrong in kind, not
  merely premature;
- **this is not an agent yet.** It executes one fixed task shape over exactly
  two evidence items; it does not decide what to check, retrieve its own
  material, or plan a sequence of steps. There are now two allowlisted
  operations rather than one, which is still a very short list;
- the document slice a run sees is now cut by a rule rather than by hand
  (`finauditgate.slicing`), but a person still has to say which row is meant.
  On the two filings used so far, every reviewed case has a candidate slice
  carrying both cited figures with their period header and currency and scale.
  Those rules were written against those same two documents, so that is a fit,
  not a generalisation, and a third filing is what would test it;
- roughly half the cases on a new filing need a human, and that is the design
  working, not a hidden cost that has been measured away;
- no reviewer disposition has been recorded for the second filing at all;
- the repaired route has not been evaluated on anything.
