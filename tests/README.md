# Tests

The current suite uses Python `unittest`, scripted candidates, temporary directories, and synthetic fixtures only. It runs without network, issuer data, model weights, or credentials.

Verified behavior currently covers:

- a scripted candidate whose frozen evidence is accepted, calculated with `Decimal`, persisted, and replayed by a fresh gate with no Model Adapter;
- a candidate whose claimed value disagrees with the frozen byte span and therefore yields replayable `HUMAN_REVIEW` rather than `ACCEPT`.
- period direction, duplicate or missing operands, output unit, and rounding-policy mismatches that cannot reach `ACCEPT`;
- immutable task/candidate snapshots, exact source/content/profile binding, and malformed lineage identifiers;
- idempotent reruns, append-only conflict refusal, and replay of an older registered policy after the current-policy pointer advances;
- forged outcome metadata, unsupported task schema, malformed or missing artifacts, and mutable process Decimal context during offline replay.

The M1 contract suite also checks the frozen public fields/signatures, core v1
schemas, product schema drafts, contract-only synthetic journey, and public
demo. This is narrow M1 evidence, not a claim that the generalized deterministic
core, FinResearchOps Application Module, or Agent is complete.
