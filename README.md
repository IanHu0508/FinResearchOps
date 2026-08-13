# FinResearchOps / FinAuditGate

> **Status:** `PARTIAL` overall; M1 `COMPLETED` by this initial immutable commit. The FinResearchOps Application Module, generalized deterministic core, real-data/model Adapters, experiments, and resume results are not complete.

FinResearchOps is the outward product: a filing-update and research-change workflow for equity researchers. FinAuditGate is its trusted core for fail-closed evidence, financial semantics, deterministic calculation, and replay. They are two layers of one project; this repository and the `finauditgate` Python package keep their existing names.

## Active MVP

```text
FinResearchOps: frozen filing → Case → analysis → Workpaper
→ human APPROVE / RETURN / REJECT → proposal-only Change Packet → replay
                          ↓
FinAuditGate: period / metric / unit → Evidence Ledger → Decimal calculation
→ ACCEPT / RETRY / ABSTAIN / HUMAN_REVIEW → offline replay
```

The external Interface is deliberately small:

```python
outcome = gate.run(task)
report = gate.replay(run_ref)
```

The current slice verifies exactly one content-bound synthetic revenue-growth profile, content-addressed artifacts, offline replay, and fail-closed `HUMAN_REVIEW` for anything outside that profile. Its `FrozenDocumentPackage` makes submitted bytes and declared metadata immutable; it does **not** prove that a source or publication date is official. Case, Review Record, Workpaper, and Change Packet have M1 schema drafts and a contract-only example, while the Application Module that would create and enforce them remains `NOT_STARTED`. This is not a runnable LLM Agent.

## Milestone progress

- Completed at planning level: two-layer identity, first user, six-step workflow, proposal-only outlet, Application/Core Seam, model route, data route, stop ceiling, non-goals, and UI deferral.
- Completed M0 decisions/setup: `Apache-2.0`, the recommended 125–140 h Resume MVP tier, user-provided repo-local Git identity, and a uv-managed Python 3.12.13 repository `.venv`.
- M0 complete: two independent read-only audits verified all M0 evidence and found no privacy/scope blocker.
- Existing inherited evidence: scripted synthetic `run()` / `replay()` round-trip, adversarial fail-closed/replay-integrity checks, and the public/private data boundary.
- M1 complete: this initial immutable commit contains the seven-symbol core Interface, versioned core schemas, four product schema drafts, one scripted product-journey mapping, a core synthetic demo, fail-closed adversarial coverage, append-only/idempotent artifacts, and historical-policy replay compatibility.
- M1 completion is narrow milestone evidence, not a completed Agent claim. The Application Module and M2 remain `NOT_STARTED`; real local-model smoke and Tencent acquisition remain M3; post-freeze Alibaba evaluation remains M4.

## Repository contents

- `src/finauditgate/`: future public package and internal Modules.
- `tests/`: synthetic-only offline verification.
- `fixtures/synthetic/`: original synthetic filings and failure cases.
- `schemas/`: four implemented core v1 contracts, four M1 product schema drafts, and a contract-only synthetic journey example.
- `manifests/examples/`: public-safe example provenance metadata.
- `docs/`: architecture, data policy, evaluation protocol, and status.
- `scripts/`: reproducible local commands added only when implemented.

## Environment rebuild and verification

The verified M0 environment uses uv-managed CPython 3.12.13 from Astral's `python-build-standalone` distributions; it is not a Python.org macOS binary. The installer is deliberately prevented from changing shell profiles:

```bash
curl -LsSf https://astral.sh/uv/0.12.3/install.sh | env UV_NO_MODIFY_PATH=1 sh
$HOME/.local/bin/uv python install 3.12.13 --managed-python
$HOME/.local/bin/uv venv --python 3.12.13 --managed-python .venv
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

The exact Python patch is also pinned in `.python-version`. Do not substitute the system Python or a Codex-managed runtime. Updating uv or Python is a deliberate environment-contract change and requires a clean rebuild plus the offline suite.

To build and verify the installed package in a disposable user-owned environment:

```bash
M1_VERIFY_ROOT="$(mktemp -d)"
$HOME/.local/bin/uv build --wheel --out-dir "$M1_VERIFY_ROOT/dist"
$HOME/.local/bin/uv venv --python 3.12.13 --managed-python "$M1_VERIFY_ROOT/venv"
$HOME/.local/bin/uv pip install --python "$M1_VERIFY_ROOT/venv/bin/python" "$M1_VERIFY_ROOT"/dist/*.whl
env -u PYTHONPATH "$M1_VERIFY_ROOT/venv/bin/python" -c 'import finauditgate; print(finauditgate.__all__)'
env -u PYTHONPATH "$M1_VERIFY_ROOT/venv/bin/python" scripts/synthetic_demo.py --artifact-root "$M1_VERIFY_ROOT/artifacts"
```

The package has no runtime dependency or network path. A first wheel build may access the configured Python package index to obtain the pinned `setuptools==84.0.0` build backend; that build-time resolution is distinct from the offline `run()` / `replay()` guarantee. Updating the backend is an explicit build-contract change and requires the clean verification sequence again.

## Explicit non-goals for the current MVP

- live web search or automated HKEX access;
- OCR or universal PDF/table parsing;
- arbitrary Python/shell execution;
- trading or automatic changes to formal research state;
- vector databases, multi-Agent orchestration, cloud deployment, or any UI before the v2 UI Entry Gate;
- SFT/RLHF/RL or paper-reproduction claims;
- production-grade security or China-market generalization claims.

## Data

The public repository will contain only synthetic fixtures and reviewed public-safe metadata. Real Tencent source files, extracted text, evaluation gold, and raw traces remain outside this Git worktree. See [data policy](docs/data-policy.md).

## License

Original code, documentation, schemas, and synthetic fixtures in this repository are licensed under the [Apache License 2.0](LICENSE). Third-party filings, issuer names and marks, model weights, and evaluation material are not relicensed; see [the data and third-party material notice](NOTICE_DATA.md).
