# Scripts

`synthetic_demo.py` runs the public synthetic profile through `run()` with a
scripted Adapter, then replays it through a fresh gate with no Adapter:

```bash
PYTHONPATH=src .venv/bin/python scripts/synthetic_demo.py --artifact-root .local/demo
```

`build_validation_profile.py` turns reviewed facts (the two document lines
that carry the values, the values and periods, the shared semantics) into the
canonical private validation profile the gate loads. Lines are found from any
unique substring and located by byte search; the vocabulary is the closed tool
schema's. It also builds the no-admissible-evidence shape:

```bash
PYTHONPATH=src .venv/bin/python scripts/build_validation_profile.py --help
```

Usage in context: `docs/runbook-private-case.md`.
