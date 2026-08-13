# Data and Third-Party Material Notice

This repository is designed to contain original FinResearchOps application
code, FinAuditGate core code, documentation, schemas, and explicitly fictional
synthetic fixtures only.

The repository's outbound code license is Apache-2.0. It applies only to
material for which this repository's authors hold the necessary rights. It
does not automatically license third-party filings, issuer names or marks,
website content, model weights, or evaluation material.

## Synthetic public material

Files under `fixtures/synthetic/` are original fictional test inputs covered by
the repository's Apache-2.0 license. They may be used for offline development
and public demonstration.

## Tencent material

Tencent reports are not distributed here. If used later, they must be acquired
manually by the user from Tencent investor relations and stored in the sibling
private workspace. Raw PDFs, extracted text, tables, screenshots, locators,
real Evidence Ledgers, and raw model traces remain private-only.

This repository must not contain a Tencent downloader or an HKEXnews crawler.

## SEC EDGAR material

The public-safe manifest in `manifests/examples/` records the intended Alibaba
Form 20-F accession only. Its status is `PLANNED_NOT_ACQUIRED`; it is not
evidence that the filing has been downloaded, cached, parsed, or evaluated.

Any future SEC acquisition tool must require a declared User-Agent with a real
contact address supplied outside committed files, use a conservative rate no
higher than one request per second, cache frozen bytes locally, and keep
`replay()` strictly offline.

## Prohibited public material

Do not commit credentials, cookies, private paths, customer or portfolio data,
issuer PDFs, extracted full text, page images, held-out gold, unreviewed model
traces, company logos, or HKEXnews-derived collections and benchmarks.
