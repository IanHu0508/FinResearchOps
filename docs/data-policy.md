# Public and Private Data Policy

## Public repository

Allowed:

- original source code and documentation;
- original synthetic filings and negative fixtures;
- schemas and public-safe example manifests;
- official source landing URLs or SEC accession metadata;
- reviewed aggregate metrics and short, necessary attributed facts.

Not allowed:

- Tencent PDFs, extracted full text, tables, page images, or embeddings;
- private paths, acquisition cookies, credentials, or browser sessions;
- held-out gold before the result-bearing run;
- unreviewed model traces containing source excerpts;
- HKEXnews crawlers, data collections, or benchmark artifacts;
- real customer, portfolio, watchlist, or transaction data.

## Local private workspace

All real source bytes, extraction artifacts, gold, exposure logs, and raw runs live in sibling `../private/`, physically outside this Git worktree.

## Acquisition

- Tencent: one-time manual user acquisition from the official investor-relations page; record URL, title, reporting period, publication date, retrieval time, byte size, MIME type, terms URL, and SHA-256.
- Alibaba evaluation: frozen SEC EDGAR accession; declared User-Agent, conservative rate limit, bounded retries, and local content-addressed cache.
- Replay: strictly offline against frozen artifacts.

