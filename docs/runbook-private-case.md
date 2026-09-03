# Runbook: one real case with the local model

This is the shortest path from a reviewed question on a real filing to a
replayable Workpaper, Review, and Packet. Every input and output stays under
the workspace's sibling `private/` tree; nothing here enters Git.

Paths below are examples. `$PRIV` is the absolute path of `private/`.

## 0. Prerequisites

```bash
ollama pull qwen3:4b-q4_K_M
ollama list          # the tag must show digest 2bfd38a7daaf…
```

The Adapter refuses to run if the installed tag carries a different digest.
If Ollama re-pulls the tag with a new digest, that is a deliberate route
change: update `MODEL_DIGEST` in `src/finauditgate/adapters/ollama_route.py`
and the public route manifest together, and expect new run identities.

The daemon version is recorded in every trace but does not gate a run.

## 1. Freeze the source

Keep the original PDF unchanged under `$PRIV/evidence/raw/<issuer>/<year>/`
and record its SHA-256, retrieval URL, and publication date in a manifest
under `$PRIV/manifests/`.

## 2. Extract text and cut one slice

Extract the native text (any tool you trust; keep the script and its hash) to
`$PRIV/evidence/extracted/…`. For each question, cut one UTF-8 slice of at
most 32,768 bytes that contains both evidence spans, for example one page or
one table plus its heading, and save it as its own file:

```text
$PRIV/datasets/dev/<issuer>/<year>/cases/<CASE-ID>/slice.txt
```

The slice is the document the model sees; its SHA-256 is the document identity
in every artifact.

## 3. Build the validation profile from reviewed facts

The profile is the answer key. Only build it from facts a reviewer has
confirmed in the PDF.

The model is bound to a closed tool vocabulary (`adapters/ollama_contract.py`),
and the profile must follow the same conventions:

- the reviewed span is the complete document line that carries the value,
  without its leading and trailing whitespace: the evidence region. The model
  may cite that whole line or just the number printed in it; the gate accepts
  a citation only inside the reviewed region and only if it carries the value.
  `--current-line` / `--comparison-line` take any unique substring of the line
  and expand it; when both values sit on one table row, both spans are that
  row. `--current-span` / `--comparison-span` take a verbatim span instead;
- `value` is the plain decimal string (`751766`, not `751,766`);
- `period` is `FY2025` for a fiscal-year flow and `2025-12-31` for a balance
  as at a date;
- metric, basis, unit, scale and sign come from the tool-schema enumerations
  (`revenue`, `REPORTED`, `MONETARY`, `MILLION`, `POSITIVE`, ...); currency is
  the abbreviation printed in the document (`RMB`).

```bash
PYTHONPATH=src .venv/bin/python scripts/build_validation_profile.py \
  --document "$PRIV/datasets/dev/tencent/2025/cases/CASE-05/slice.txt" \
  --source-id tencent-holdings-2025-annual-report \
  --document-name tencent-2025-annual-report__p130__CASE-05.txt \
  --published-at 2026-04-09 --cutoff 2026-05-01 \
  --question "What was Tencent's FY2025 revenue growth versus FY2024?" \
  --current-line "751,766" --current-value 751766 --current-period FY2025 \
  --comparison-line "660,257" --comparison-value 660257 --comparison-period FY2024 \
  --metric revenue --currency RMB --scale MILLION \
  --output "$PRIV/profiles/tencent-2025/CASE-05.json"
```

Spans are located by unique byte search; if a line occurs twice, use
`--current-span` with a longer verbatim span. Pass the same `--document-name`
to `create-case`; the profile and the task must name the document identically.

For a question whose reviewed answer is "this document does not support it",
build a no-evidence profile instead:

```bash
… --no-admissible-evidence --output "$PRIV/profiles/tencent-2025/CASE-09.json"
```

For a post-cutoff case, set `--published-at` later than `--cutoff`; the gate
will return `HUMAN_REVIEW / POST_CUTOFF_DOCUMENT` whatever the model proposes.

## 4. Run the six actions

```bash
ROOT="$PRIV/runs/dev/tencent-2025"
TRACES="$PRIV/model-traces"

finresearchops --artifact-root "$ROOT" create-case --mode PRIVATE_DEV \
  --document "$PRIV/datasets/dev/tencent/2025/cases/CASE-05/slice.txt" \
  --document-name tencent-2025-annual-report__p130__CASE-05.txt \
  --source-id tencent-holdings-2025-annual-report --published-at 2026-04-09 \
  --cutoff 2026-05-01 --question "What was Tencent's FY2025 revenue growth versus FY2024?"
# -> {"case_ref":"case-…","status":"CREATED",…}

finresearchops --artifact-root "$ROOT" run-analysis --case-ref case-… \
  --model-trace-root "$TRACES" \
  --validation-profile "$PRIV/profiles/tencent-2025/CASE-05.json"
# -> {"machine_decision":"ACCEPT"|"RETRY"|"ABSTAIN"|"HUMAN_REVIEW","reason_codes":[…],"run_ref":{"run_id":"…"},…}

finresearchops --artifact-root "$ROOT" inspect-case --case-ref case-… --model-trace-root "$TRACES"
finresearchops --artifact-root "$ROOT" review --case-ref case-… --run-id … \
  --action APPROVE --reason "Checked both spans against page 129." --model-trace-root "$TRACES"
finresearchops --artifact-root "$ROOT" export --case-ref case-… --run-id … --model-trace-root "$TRACES"
finresearchops --artifact-root "$ROOT" replay --run-id … --model-trace-root "$TRACES"
```

`review --action APPROVE` is refused unless the machine decision is `ACCEPT`;
`export` is refused unless the latest run is `ACCEPT`, approved, and replays
consistently. A non-`ACCEPT` run can be returned (`RETURN`) and re-run.

The `source_id` must match the profile's `source_id`, and the question and
dates must match the profile exactly; any difference is a
`HUMAN_REVIEW / *_PROFILE_CONFLICT`, by design.

## 5. Record the raw counts

For a batch of cases, record per case: case id, failure class, machine
decision, reason codes, whether replay was consistent, and the human action.
Report aggregate counts only (how many `ACCEPT`, how many of each failure
class, how many `unsafe_accept`, meaning an `ACCEPT` a reviewer later judged
wrong). Small samples are counts, not percentages.

## 6. Paired baseline (optional)

`finauditgate.evaluation.PairedRunner` stores two arms' actual output bytes
and a task identity, then accepts append-only human QA that classifies each
pair as wrong-to-right, right-to-wrong, right-to-right, or wrong-to-wrong.
The gated arm is the run above; the baseline arm is any ungated call whose
output bytes you keep. See `tests/test_paired_runner.py` for the contract.
