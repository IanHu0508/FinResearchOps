# Public source and model manifests

The example manifests define reproducible source selection and model-route configuration using public metadata and hashes.

`alibaba_fy2026_20f_plan.json` records an intended SEC accession and acquisition constraints. Its `PLANNED_NOT_ACQUIRED` state identifies it as an acquisition plan.

`qwen3_8b_ollama_route.json` describes the frozen local route: model tag and digests, source and license links, prompt/schema/generation hashes, byte and token budgets, and the observed daemon version.

Source bytes, model responses and evaluation keys are stored separately in the private workspace under the [data policy](../../docs/data-policy.md).
