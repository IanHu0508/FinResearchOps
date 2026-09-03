# Evaluation Protocol

> No result-bearing run has occurred. See [`status.md`](status.md).

## Splits

- `SYNTHETIC_DEV`: public original fixtures for core and adversarial testing.
- `PRIVATE_DEV`: manually reviewed private cases on a user-acquired official
  filing; each case has a reviewed validation profile.
- `POST_FREEZE_EVAL`: tasks on a second filing run only after code, prompt,
  model, schema, budget, and manifest freeze. The CLI does not expose this mode.

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

## Reporting

Report raw counts: `ACCEPT`, each failure class, `unsafe_accept` (an `ACCEPT`
a reviewer later judged wrong), replay-consistent runs, and paired
wrong-to-right / right-to-wrong counts. Do not inflate small samples into
percentages, and never tune the prompt or rules on evaluation items after a
freeze.
