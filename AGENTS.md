# Repository rules

This directory is the only public Git worktree of the project. Everything in
it must be safe to publish.

## Read first

1. `docs/status.md` is the only status source in this repository. Do not
   restate status, test counts, hashes, or verdicts in other documents;
   link here instead.
2. `docs/architecture.md` describes the mechanism; `docs/cli.md` the CLI;
   `docs/runbook-private-case.md` how to run one real case.

## Interfaces

- FinAuditGate keeps exactly `run(task)` and `replay(run_ref)`.
- FinResearchOps keeps exactly `handle(command)` and `read_case(case_ref)`.
- Callers, the CLI, and tests cross the same Interfaces; nothing reimplements
  evidence, semantic, formula, gate, review, or export rules.
- Machine `ACCEPT` never means human `APPROVE`; the Application may not bypass
  a core hard failure.
- Models propose candidates only; deterministic validators decide.
- Each persisted artifact has exactly one schema version. When a shape
  changes, bump that artifact's version and delete the old branch; do not keep
  read-only compatibility for formats that were never released.

## Dependencies and data

- Python `>=3.12,<3.13`; the core stays standard-library only.
- Tests are offline: no network, model, account, or issuer data. Loopback
  HTTP servers and mocked exchanges are fine.
- Private material (issuer documents, extracted text, profiles, raw traces,
  QA, paired outputs) lives in the sibling `../private/` tree and is never
  copied, symlinked, or referenced by absolute path from this repository.
- Only original code, synthetic fixtures, schemas, public-safe manifests, and
  reviewed aggregate counts may be committed.

## Verification

Run the full suite before and after any change:

```bash
PYTHONPATH=src .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
```

For a release candidate also build the wheel and run the suite from the
isolated install (commands in `README.md`). That is the whole ritual; there
is no separate freeze-and-audit loop.

## Claims

Do not report performance, evaluation, held-out, deployment, or reproduction
claims before the artifacts exist. Preserve failed items; never delete a
negative result.

## Git

No remote, commit, or push without explicit user authorization. Do not change
the Apache-2.0 license. Before any push, scan for private paths, secrets,
PDFs, long source excerpts, held-out gold, and raw model traces.
