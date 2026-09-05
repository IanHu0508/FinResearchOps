# Evaluation Protocol

> Counts and the record of every evaluation live in [`status.md`](status.md).
> This file holds the rules; it was written before any result-bearing run and
> has since gained the rules those runs taught.

## Splits

- `SYNTHETIC_DEV`: public original fixtures for core and adversarial testing.
- `PRIVATE_DEV`: manually reviewed private cases on a user-acquired official
  filing; each case has a reviewed validation profile.
- `POST_FREEZE_EVAL`: tasks on a second filing, run only after code, prompt,
  model, schema, budget, and manifest freeze. It is decided through exactly the
  checks `PRIVATE_DEV` uses — a reviewed validation profile is required, the
  artifact root and the model trace must be private, and the same gate rules
  apply — so the split changes what a run is *called*, never how strictly it is
  judged. The profile declares which split it may decide (`accepted_mode`), and
  a task in the other reviewed mode is refused `MODE_CONFLICT`, so a development
  answer key can never decide a transfer run or the reverse. Because the mode is
  recorded in every artifact and in the model's own user message, development
  and post-freeze results can be told apart afterwards and must never be pooled.
  One consequence to state when reporting: the mode string is part of the prompt
  bytes, so a transfer run's user message is not byte-identical to a development
  run's.

## Truthful naming

Without independent custody of questions, gold, rubric, and locators, call a
later transfer run an "issuer-level post-freeze transfer evaluation", never a
strict blind held-out or a benchmark.

## Required artifacts per run

- Git commit identity and diff;
- source and dataset hashes;
- observed model tag digest and daemon version (from the trace);
- prompt, response schema, generation config, and calculation-policy hashes;
- the raw trace, parsed candidate, attempt sequence, final decision, and
  latency;
- evidence ledger, Workpaper, replay report, and manual QA;
- every right-to-wrong regression and protocol deviation.

## Case design — rules learned from running this

Each rule below exists because its absence cost a run. A pack that violates one
does not get sealed; the pre-seal check enforces the ones a program can.

1. **A case must be buildable before it is sealed.** Every locator is resolved
   at pack-build time with the same function the run uses. Two cases in one
   evaluation never reached a model because their locator was the bare printed
   figure and that figure appeared twice in the slice; the run log showed a case
   header and nothing else. A figure printed more than once is named by its row,
   or by which printing it is.
2. **A slice must state what its claim is judged on.** Period, currency unless
   the claim declares none, scale unless the amount is per share — each checked
   separately. Half the cases in one evaluation were unanswerable from their own
   slice, because the check asked for *currency or scale* — an `or` between two
   things that are not interchangeable is not a check.
3. **A prediction about what a model writes is scored against the field it
   writes, not against the gate's reason code.** A reason code is a decision
   about a claim, not the claim. Scored the wrong way, one repair read as four
   successes in five; scored the right way it was two in four.
4. **A question may not hand over a field being scored.** For a percentage the
   scale never needs naming, and a question that names it is malformed. For a
   difference the answer is a raw number, so the question must say what unit it
   wants — every such question does — and the case records
   `scale_disclosed_by_question` and is excluded from any claim about reading
   scale from the document. This asymmetry is a property of the two operations.
5. **A growth-rate question may not say "change".** The contract tells the model
   that *change* means the difference operation; a growth question worded that
   way asks for one thing and is scored on another, and the model obeying the
   contract fails the case.
6. **The metric label is the reviewer's to get right, and nothing lints it.**
   One case failed because the model chose the more specific of two labels and
   the reviewer had chosen the less specific one. The model was right.
7. **A flag with no test has not been run.** One locator flag raised an
   exception on every call it had ever received and survived because nothing
   exercised the script that offered it.
8. **What the model is shown is part of the measurement.** Preparation — the
   slicer, the operation table, the profile builder — changes the input without
   changing the route hash, and one repair to it slipped between two evaluations
   unmeasured. The freeze pins those files too.

## Reporting

Report raw counts: `ACCEPT`, each failure class, `unsafe_accept` (an `ACCEPT`
a reviewer later judged wrong), replay-consistent runs, and paired
wrong-to-right / right-to-wrong counts. Do not inflate small samples into
percentages, and never tune the prompt or rules on evaluation items after a
freeze.
