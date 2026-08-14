# Adapters

Current and planned Adapter order:

1. scripted candidate Adapter for deterministic tests — implemented for the
   exact M1 profile and bounded M2 attempt sequences;
2. one local Qwen Adapter only in a separately authorized M3 — `NOT_STARTED`;
3. real filing acquisition/parsing Adapters only after source and license review — `NOT_STARTED`.

The current synthetic documents are submitted through the public
`FrozenDocumentPackage` value; it is not a source Adapter and does not prove
source authenticity.

Optional dependencies must remain inside their Adapter implementations.
