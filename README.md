# FinResearchOps / FinAuditGate

A filing-update workflow for equity researchers, built around one idea: **the
model may only propose; deterministic code verifies and calculates; a person
approves; everything replays offline.**

- **FinAuditGate** is the core. Given one frozen document and one question, it
  takes a candidate from a model (two evidence spans, their financial
  semantics, one allowlisted formula), checks every claim against the frozen
  bytes and a reviewed profile, computes the answer with `Decimal`, and returns
  exactly one of `ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW`. Every run is a set
  of append-once, content-addressed artifacts that a second gate with no model
  can replay.
- **FinResearchOps** is the workflow around it: a Case is created from a frozen
  filing, analysed once, inspected as a Workpaper, reviewed by a person
  (`APPROVE / RETURN / REJECT`), and only then exported as a proposal-only
  Research Change Packet.

```text
frozen text slice (≤ 32 KB)
   → local model (Qwen3-4B via Ollama, one forced tool call)
   → FinAuditGate: span/hash check, semantics, Decimal, one retry
   → ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW
   → FinResearchOps: Case → Workpaper → human review → Change Packet
   → offline replay (no model, no network)
```

Current status lives in one place: [`docs/status.md`](docs/status.md).

## Quick start

The package is standard-library only. The verified environment is a
uv-managed CPython 3.12.13 with a repository `.venv` (see `.python-version`).

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Run the public synthetic profile end to end and replay it without a model:

```bash
PYTHONPATH=src .venv/bin/python scripts/synthetic_demo.py --artifact-root .local/demo
```

Build and verify the installed package in a disposable environment:

```bash
W="$(mktemp -d)"
$HOME/.local/bin/uv build --wheel --out-dir "$W/dist" --offline
$HOME/.local/bin/uv venv --python 3.12.13 --managed-python "$W/venv"
$HOME/.local/bin/uv pip install --python "$W/venv/bin/python" --offline "$W"/dist/*.whl
env -u PYTHONPATH "$W/venv/bin/finresearchops" --help
```

## Running a real case with the local model

`finresearchops` is a thin CLI over the Application Interface
(`handle(command)` / `read_case(case_ref)`). It needs a local Ollama daemon
with the frozen tag installed, and every private input must live under the
workspace's sibling `private/` tree:

```bash
ollama pull qwen3:4b-q4_K_M
finresearchops --artifact-root /…/private/runs/dev/example \
  create-case --mode PRIVATE_DEV --document /…/private/…/slice.txt \
  --source-id tencent-2025-annual-report --published-at 2026-04-09 \
  --cutoff 2026-05-01 --question "…"
finresearchops --artifact-root /…/private/runs/dev/example \
  run-analysis --case-ref case-… --model-trace-root /…/private/model-traces \
  --validation-profile /…/private/profiles/case-01.json
```

The complete procedure, including how to build the validation profile from
reviewed facts, is in [`docs/runbook-private-case.md`](docs/runbook-private-case.md).
The CLI actions are documented in [`docs/cli.md`](docs/cli.md).

## What is and is not proven

Proven by the offline suite (131 tests, no network, no model, no issuer data):

- the deterministic gate on a public synthetic profile, including registered
  fiscal-period / metric / basis / currency / unit / scale / sign aliases,
  bounded one-retry behaviour, and fail-closed handling of malformed proposals;
- the Application lifecycle with append-only Reviews, proposal-only export, and
  crash recovery at every publication boundary;
- the local-model Adapter contract with mocked loopback exchanges: one frozen
  request, bounded raw capture, one content-addressed trace per call, and an
  offline verifier that reconstructs the proposal from the saved bytes;
- private validation profiles for acceptable answers, post-cutoff documents,
  and questions with no admissible evidence, through to Packet export.

Not proven yet:

- any `ACCEPT` on a real issuer document with the real local model;
- generalisation beyond one formula (`growth_rate_percent`) and hand-cut text
  slices (there is no PDF parsing or retrieval inside the product);
- any evaluation result.

## Repository layout

- `src/finauditgate/` — core (`core/`), Application (`application/`), model
  Adapters (`adapters/`), the paired-evaluation helper (`evaluation/`), CLI.
- `tests/` — offline `unittest` suite.
- `fixtures/synthetic/` — the one original synthetic filing fragment.
- `schemas/` — JSON Schemas for every persisted artifact, one version each.
- `manifests/examples/` — public-safe route and source metadata.
- `scripts/` — the synthetic demo and the validation-profile builder.
- `docs/` — architecture, status, CLI, runbook, data policy, evaluation protocol.

## Data

The repository contains only original code, synthetic fixtures, schemas, and
public-safe metadata. Issuer documents, extracted text, validation profiles,
raw model traces, and manual QA stay in the sibling `private/` directory and
never enter Git. See [`docs/data-policy.md`](docs/data-policy.md) and
[`NOTICE_DATA.md`](NOTICE_DATA.md).

## License

Original code, documentation, schemas, and synthetic fixtures are licensed
under the [Apache License 2.0](LICENSE). Third-party filings, issuer names and
marks, model weights, and evaluation material are not relicensed.
