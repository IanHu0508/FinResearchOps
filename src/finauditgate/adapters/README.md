# Adapters

Two implementations of the `CandidateModel.propose` Seam:

1. `scripted.py` returns predeclared candidates or attempt sequences; it is
   how the deterministic core is tested without a model.
2. `ollama.py` calls the one frozen local route defined in `ollama_route.py`
   (Qwen3-4B through a loopback Ollama daemon), captures the raw bytes with a
   fixed budget, and writes one content-addressed trace per call. The response
   codec in `ollama_trace.py` and the tool schema/decoder in
   `ollama_contract.py` are shared with the offline verifier, so a saved trace
   can only ever claim the proposal its own bytes produce.

The tool schema is closed: evidence ids are `current` and `comparison`;
metric, basis, unit, scale and sign are enumerations; `value` is a plain
decimal string; `period` is `FYyyyy` or `yyyy-mm-dd`; `exact_span` is the
number as printed or the complete document line that carries it, and must be
unique in the document. The decoder rejects anything outside that vocabulary
before the core sees it, so the model and the reviewed profiles speak the same
words and the gate never has to guess what a label meant.

The Adapter refuses redirects, ignores environment proxies, requires the
installed tag to carry the frozen digest, and records the daemon version
without gating on it. A shared daemon cannot prove which model bytes produced a
response; the trace records what was observed and claims nothing more.

The local runtime is an external application, not a Python dependency of the
package. Real-filing acquisition or parsing Adapters do not exist.
