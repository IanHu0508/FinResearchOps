# Model and source adapters

Adapters connect external models, the TradingAgents graph and source acquisition to the research workflow. The main cloud path is implemented by `tradingagents_thesis.py`, with stage protocols, schemas and bounded recovery in the adjacent thesis modules.

## Retained candidate-generation interface

`CandidateModel.propose` has two implementations:

1. `scripted.py` returns fixed candidates and attempt sequences for deterministic tests.
2. `ollama.py` calls the Qwen3-8B loopback route in `ollama_route.py`, captures response bytes under a fixed budget and writes a content-addressed trace.

The response codec and offline verifier share `ollama_trace.py` and `ollama_contract.py`. A saved trace binds the candidate to the captured response.

The closed response schema specifies `current` and `comparison` evidence IDs, enumerated financial semantics, a plain-decimal `value`, a `FYyyyy` or `yyyy-mm-dd` period, and `exact_span`. The same schema is sent as the runtime's response format and used when decoding.

Source location first checks exact bytes, then a unique whitespace-tolerant match. The ledger always records the document's own offsets. Ambiguous locations produce `EVIDENCE_SPAN_NOT_UNIQUE`.

The loopback adapter verifies the installed model tag and frozen digest, bounds captured responses and records the observed daemon version. Its transport uses direct loopback access with redirect and proxy checks. Local-model execution and cloud research use their separately prepared runtimes.
