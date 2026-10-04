# Runnable examples and installation checks

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

The current research example is `thesis_offline_demo.py`: scripted synthetic replies pass through parameter revision, financial recomputation, report binding and Case reopening. `--bad-prose` exercises the unbound-number control. See the [main README](../README.md#try-the-current-research-workflow-offline) for the command.

`verify_installed.py` runs the core suite from an isolated wheel, checks installed source bytes and removes source imports from the test workspace. The [CI workflow](../.github/workflows/offline.yml) uses it alongside native integration and Quant contract checks.

Validation-profile usage is documented in the [private-case runbook](../docs/runbook-private-case.md).
