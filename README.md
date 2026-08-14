# FinResearchOps / FinAuditGate

> **Status:** `PARTIAL` overall. M1 is `COMPLETED` at immutable commit
> `8eae1dd`; the bounded scripted M2 slice is `COMPLETED` after renewed full-M2
> Standards/Spec review, source and isolated clean-wheel verification, and
> closure of the three reopened defects. The M2 candidate is locally
> hash-frozen but not committed or released. Real-data/model Adapters, issuer work,
> evaluation, UI, and resume admission remain incomplete.

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

The public core Interface remains exactly `run()` / `replay()`. M2 adds one
second content-bound original synthetic profile with versioned fiscal-period,
metric, basis, currency, unit, scale, and sign registries; a bounded one-retry
proposal/candidate attempt sequence; deterministic Decimal lineage; and
replay/v2. The root
package still exports only the seven frozen M1 core symbols.

The `finauditgate.application` subpackage now implements the M2
FinResearchOps Interface:

```python
application_outcome = application.handle(command)
case_view = application.read_case(case_ref)
```

It derives Case state from an integrity-checked append-only event journal,
keeps machine decisions separate from human review, and on covered eligible
normal paths exports only a replay-verified `proposal_only=true` Change Packet.
The completed M2 candidate treats the journal head as the committed visible
prefix, never treats a pending intent or commit receipt as command authority,
and passes the bounded independent crash-recovery and membership re-audits.
`FrozenDocumentPackage`
freezes submitted bytes and declared metadata; it does **not** prove that a
source or publication date is official. This remains a scripted synthetic
runtime, not a runnable LLM Agent or a real filing workflow.

## Milestone progress

- Completed at planning level: two-layer identity, first user, six-step workflow, proposal-only outlet, Application/Core Seam, model route, data route, non-goals, and UI deferral.
- Completed M0 decisions/setup: `Apache-2.0`, user-provided repo-local Git identity, and a uv-managed Python 3.12.13 repository `.venv`. The former 125–140 h planning tier is retained only as history; work hours are no longer Gate evidence.
- M0 complete: two independent read-only audits verified all M0 evidence and found no privacy/scope blocker.
- Existing inherited evidence: the M1 scripted synthetic `run()` / `replay()` round-trip, adversarial fail-closed/replay-integrity checks, and the public/private data boundary.
- M1 complete: this initial immutable commit contains the seven-symbol core Interface, versioned core schemas, four product schema drafts, one scripted product-journey mapping, a core synthetic demo, fail-closed adversarial coverage, append-only/idempotent artifacts, and historical-policy replay compatibility.
- M2 completed bounded slice: registered semantic normalization and ambiguity,
  bounded retry/stop rules, replay/v2, formal Application schemas, `handle` /
  `read_case`, append-only Review, proposal-only Packet export, and the scripted
  journey pass the renewed full-M2 Gate. This completion is limited to the
  scripted synthetic M2 contract.
- M2 completion is not a completed-Agent claim. Real local-model smoke and
  Tencent acquisition are `NOT_STARTED` M3 work requiring separate
  authorization; post-freeze Alibaba evaluation remains M4.

## Repository contents

- `src/finauditgate/`: the public core package plus the M2 Application Module.
- `tests/`: synthetic-only, network-free core and Application verification.
- `fixtures/synthetic/`: original synthetic filings and failure cases.
- `schemas/`: core replay contracts, M1 historical drafts, and formal M2 Application artifact contracts.
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
