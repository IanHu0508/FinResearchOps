# Data and Third-Party Material Notice

This repository contains original FinResearchOps application code,
FinAuditGate core code, documentation, schemas, and explicitly fictional
synthetic fixtures only.

The outbound code license is Apache-2.0. It applies only to material for which
this repository's authors hold the necessary rights. It does not license
third-party filings, issuer names or marks, website content, model weights, or
evaluation material.

## Synthetic public material

Files under `fixtures/synthetic/` are original fictional test inputs covered
by the Apache-2.0 license. They may be used for offline development and public
demonstration.

## Issuer material

Issuer reports are not distributed here. Any official filing used for
development is acquired manually by the user, stored in the sibling private
workspace, and never copied into this repository. Raw PDFs, extracted text,
tables, page images, locators, validation profiles, ledgers, and raw model
traces remain private-only. This repository must not contain a downloader or
crawler for issuer or exchange websites.

## SEC EDGAR material

The public-safe manifest in `manifests/examples/` records an intended Form
20-F accession only. Its status is `PLANNED_NOT_ACQUIRED`. Any future SEC
acquisition tool must declare a User-Agent with a real contact address supplied
outside committed files, keep to at most one request per second, cache frozen
bytes locally, and keep `replay()` offline.

## Local model material

Qwen and Ollama are third-party components and are not relicensed by this
repository. The public route manifest records identifiers, hashes, licenses,
and official links only. Model weights, the Ollama application, local caches,
and raw request/response traces stay outside Git.

Runtime path checks require private artifacts, validation profiles, model
traces, and paired outputs to resolve below the workspace's sibling `private/`
tree. Change Packet export contains neither raw requests/responses nor private
trace paths.

## Optional TradingAgents integration

The integration depends on [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
at the commit pinned in `requirements/tradingagents.lock`. Its source is
installed separately, not vendored in this repository; its Apache-2.0 license
and third-party notices remain applicable. DeepSeek is an external model
service. Provider responses, data caches and real research reports stay private.
This project is an independent integration and does not imply upstream endorsement.

## Prohibited public material

Do not commit credentials, cookies, private paths, customer or portfolio data,
issuer PDFs, extracted full text, page images, held-out gold, raw model traces,
company logos, or exchange-derived collections.
