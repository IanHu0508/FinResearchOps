# Example Manifests

Public examples contain only safe official-source metadata and hashes. They
must never include private paths, credentials, hidden gold, or source excerpts.

`alibaba_fy2026_20f_plan.json` freezes an intended SEC accession and
acquisition constraints. Its `PLANNED_NOT_ACQUIRED` state means it is not a
download record, cache manifest, or evaluation artifact.

`qwen3_8b_ollama_route.json` records the one frozen local-model route: model
tag and digests, public license/source links, the prompt/tool/generation-config
hashes the Adapter and the offline verifier share, byte and token budgets, and
the daemon version observed when the route was smoke-tested. It contains no
weights, raw responses, private paths, or results.
