# Tests

The suite uses Python `unittest`, temporary directories, the one synthetic
fixture, scripted candidates, and mocked or loopback-only HTTP exchanges. It
needs no network, model, account, or issuer data.

```bash
PYTHONPATH=src .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
```

| File | Covers |
|---|---|
| `test_public_contracts.py` | the frozen root exports and dataclass fields, schema files, the synthetic demo script |
| `test_core_gate.py` | the deterministic gate: registered aliases, one-retry stop rules, every decision class, malformed proposals, artifact tampering, idempotent append-only runs, fresh-process replay |
| `test_application.py` | Case lifecycle, human review, proposal-only export, crash recovery at every publication boundary, foreign artifacts, concurrency |
| `test_cli.py` | thin command dispatch, stable path-free errors, private-path enforcement |
| `test_ollama_tool_contract.py` | the single response schema/decoder and span location |
| `test_ollama_adapter.py` | the frozen route on mocked loopback exchanges: one trace per call, negative captures, redirect/proxy refusal, offline verification, frozen-route binding |
| `test_model_trace_binding.py` | a trace must cause the same proposal before `ACCEPT`; Workpaper/Packet binding; missing, tampered, cross-run traces |
| `test_private_dev_profile.py` | private profiles: acceptable answer through export, post-cutoff, no admissible evidence, locator/span/value/period/metric/operand attacks |
| `test_private_storage.py` | the sibling `private/` boundary and single workspace anchor |
| `test_paired_runner.py` | paired outputs stored before append-only human QA |

Passing this suite is evidence about the mechanisms above only. It is not an
evaluation result and says nothing about real issuer documents.
