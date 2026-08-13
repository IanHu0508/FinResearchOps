# Scripts

`synthetic_demo.py` is a runnable, network-free demonstration of the exact M1
FinAuditGate profile. It calls the public `run()` entry with a scripted Adapter,
then calls public `replay()` through a fresh gate with no model Adapter:

```bash
PYTHONPATH=src .venv/bin/python scripts/synthetic_demo.py \
  --artifact-root .local/synthetic-demo
```

This is a core demo, not the FinResearchOps product CLI. The product CLI is
only a contract draft in `docs/cli-contract.draft.md`; its Application Module
does not exist yet. SEC acquisition, evaluation, and public-release scripts are
also not implemented.
