# Cash-flow investigation

Status and measured results live in [status.md](status.md).

The user task is to investigate whether changes in consolidated earnings are
supported by operating cash flows, and find filing disclosures relevant to
the largest adjustments. The output is a local Chinese workpaper with source
links, missing inputs and the actual investigation steps.

## The complete path

1. `FinResearchOps.handle(InvestigateCashflow(task))` accepts an acquired
   filing and declared source metadata, comparative annual periods and currency.
2. `FinAuditGate.run(CashflowTask)` reads inline-XBRL facts from those bytes.
   It does not accept or load an answer profile. The reader selects a
   consolidated cash-flow table with `ProfitLoss` and operating cash flow;
   entity, duration and currency must match. `NetIncomeLoss` (parent income)
   is not silently substituted for consolidated profit.
3. The core calculates the two-period differences and cash-flow reconciliation.
   Adjustment effects use the statement's displayed sign, which can differ
   from the XBRL taxonomy sign. The rounding allowance comes from the facts'
   `decimals` attributes. A missing comparative can use an explicit same-filing
   fact only when concept, entity, full period and currency match. Conflicting
   candidates fail closed; a nonzero value additionally needs a cash-flow sign
   relation from the existing row. A dash alone or a zero residual never supplies
   a missing zero. The evidence page distinguishes directly read and supplemented facts.
   When the statement explicitly labels changes in operating assets and liabilities,
   that section is summed separately, with missing-item coverage retained.
4. A rule planner or the fixed local 8B planner chooses a driver from the
   available IDs. Only `SEARCH_NOTES` and `FINISH` are accepted. At most two
   searches run. The next observation includes previous hits and failure
   feedback. Repeated or invalid actions stop; there is no shell, arbitrary
   code execution, external search or model-selected filesystem path. Before
   those optional searches, both routes read the same operating-cash-flow overview.
   This is one deterministic overview lookup plus at most two follow-up searches.
   Paragraphs and leaf divs retain nearby heading references. Topic matching handles
   plural financial terms; ranked evidence distinguishes current-period changes,
   other periods, policies and conditional risk text. These remain documentary
   candidates, not accepted causal explanations.
   If an adaptive search finds only background material or no matches, the
   remaining search uses the highest-priority unvisited driver without another
   model call. Each recorded step distinguishes `MODEL`, `RULES` and
   `RULE_FALLBACK`; replay verifies the origin and action instead of crediting
   the fallback to the model.
5. The Application saves an immutable draft Case, `workpaper.html` and
   `evidence.html`. These stay under private storage and can be reopened with
   `read_case`. Original text is escaped and the HTML has no scripts.
6. `FinAuditGate.replay` rereads the captured source, recomputes the financial
   checks and searches, and verifies recorded model responses without calling
   the model. `handle(ReplayRun)` also works for this task.

The existing `handle/read_case` and `run/replay` methods are the only external
execution Interfaces. Task-specific types are in `finauditgate.cashflow`.
The four internal responsibilities are source reading (`core/ixbrl.py`),
financial checks and orchestration (`core/cashflow.py`), the local planner
Adapter (`adapters/cashflow_ollama.py`), and Application persistence/rendering
(`application/cashflow_case.py`, `application/cashflow_report.py`).

## Run one acquired filing

Use absolute paths below the same workspace's `private/` directory:

```bash
PYTHONPATH=src .venv/bin/python -m finauditgate.cli \
  --artifact-root <absolute-private-output-directory> \
  investigate-cashflow \
  --source-manifest <absolute-acquisition-provenance-json> \
  --comparison-end 2024-12-31 --cutoff 2026-09-05 \
  --currency CNY --strategy rules
```

`--strategy adaptive` uses the already installed `qwen3:8b-q4_K_M` route.
Nothing is installed or downloaded by the command. Both strategies use the
same financial checks, document, search tool and two-search budget.

The acquisition manifest supplies `stored_as`, `issuer`, `source_url`,
`accession`, `cik`, `publication_date_filed`, `reporting_period_end`, `sha256`,
`data_class: PUBLIC_SOURCE_LOCAL` and `form: 20-F`. It describes where the
filing came from; it does not contain expected financial values. The CLI
checks the original file hash before starting. An alternative explicit-metadata
mode is listed by `investigate-cashflow --help`.

The response contains the Case reference, run ID and workpaper path. Use
`inspect-case --case-ref ...` to reopen it and `replay --run-id ...` with the
same artifact root to recompute it offline. Cash-flow raw model responses are
bound inside the private run record, so no separate trace-root flag is needed.

## Deliberate limits

- This reader handles an explicit subset of inline-XBRL annual consolidated
  US-GAAP statements. It is not a conformant general XBRL processor, and does
  not parse PDF, IFRS, dimensional/segment data or untagged numeric cells.
- Periods use actual start/end dates, monetary facts use explicit currency
  units, and unknown numeric transformations are rejected. Untagged dashes
  and blank cells are not silently interpreted as zero. An explicit zero
  tagged elsewhere is distinguishable from an untagged dash in the target row.
- Source positions are Unicode character offsets in the UTF-8-decoded
  filing, with a hash of the exact source substring; these are not byte offsets.
- Note matches are lexical candidates. The planner selects an investigation
  direction, not an accepted causal explanation. No claim of superior
  investigation quality follows from a successful model call.
- When a current-period overview explicitly quantifies a working-capital
  movement in the selected currency, the core can compare it with the statement's
  operating-assets/liabilities section at the disclosed rounding precision.
  Numeric compatibility does not establish identical definitions or causation.
  The original quoted statement, computed amount and difference remain visible.
- Every cash-flow result is a research draft awaiting human review. The new
  draft path does not implement approval or public Change Packet export;
  the older reviewed-profile path retains its own approval/export workflow.
- The shared Ollama digest is observed, not request-level execution attestation.
  The new task has no runtime gold input; this is not a claim of operating-system
  isolation from all unrelated host files.

`core/disclosures.py` contains the topic/period/documentary-role rules used by
both overview and follow-up searches. The older cash-flow run format is preserved
as historical files; it is not accepted by the current replay implementation.

## Financial and format references

The distinction between consolidated `ProfitLoss` and parent `NetIncomeLoss`
is documented in the [FASB taxonomy implementation guide](https://xbrl.fasb.org/impdocs/OCI_TIG/othercompincome.htm).
Numeric transforms, `contextRef`, `unitRef`, `scale` and `sign` are specified
by [XBRL International](https://specifications.xbrl.org/work-product-index-inline-xbrl-inline-xbrl-1.1.html).
The reader uses a documented subset of those mechanisms, not full conformance.
