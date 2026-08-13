# Adapters

Current and planned Adapter order:

1. scripted candidate Adapter for deterministic tests — implemented for the exact M1 profile;
2. one local Qwen Adapter after the core passes its later Gate — `NOT_STARTED`;
3. real filing acquisition/parsing Adapters only after source and license review — `NOT_STARTED`.

The current synthetic document is submitted through the public
`FrozenDocumentPackage` value; it is not a source Adapter and does not prove
source authenticity.

Optional dependencies must remain inside their Adapter implementations.
