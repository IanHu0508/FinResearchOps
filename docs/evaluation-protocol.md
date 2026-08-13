# Evaluation Protocol

> Status: planned, no result-bearing run has occurred.

## Splits

- `SYNTHETIC_DEV`: public original fixtures for core and adversarial testing.
- `PRIVATE_DEV`: manually reviewed Tencent cases.
- `POST_FREEZE_EVAL`: Alibaba SEC filing tasks run after code, prompt, model, schema, budget, and manifest freeze.

## Truthful naming

Without independent custody of questions, gold, rubric, and locators, use:

```text
issuer-level post-freeze transfer evaluation
```

Do not use `strict blind held-out`, `pristine benchmark`, or `China-market generalization`.

## Required artifacts

- Git commit/diff identity;
- source and dataset hashes;
- model/runtime resolved identity;
- prompt, schema, tool, budget, and calculation-policy hashes;
- raw output, parsed candidates, tool decisions, final state, latency, and resource/cost record;
- evidence ledger, workpaper, replay report, and manual QA;
- every right-to-wrong regression and protocol deviation.

FinResearchOps product artifacts will additionally require a Case Record,
Workpaper, append-only Review Record, and proposal-only Research Change Packet.
Their M1 schema drafts exist, but their Application Implementation and runtime
cross-object enforcement are `NOT_STARTED`; listing or validating a schema is
not evidence that an application workflow or human-review loop exists.

Public claims require raw-count reporting and resume-admission review; small samples must not be inflated into broad percentages.
