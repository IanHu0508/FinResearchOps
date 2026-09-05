"""Build one canonical PRIVATE_DEV validation profile from reviewed facts.

A profile is the reviewed answer key the gate compares a model proposal
against.  This script turns the facts a reviewer has already confirmed (the
two evidence lines, their values and periods, the shared semantics) into the
canonical JSON the gate loads.  It locates each span by unique byte search, so
the reviewer never types byte offsets by hand.

The profile must follow the same conventions the closed tool schema imposes on
the model (`finauditgate.adapters.ollama_contract`):

* the reviewed span is the complete document line that carries the value,
  without its leading and trailing whitespace; the model may cite that line or
  just the number inside it, and the gate accepts citations only inside the
  reviewed span. `--current-line` / `--comparison-line` take any unique
  substring of the line and expand it (when both values sit on one table row,
  both spans are that row); `--current-span` / `--comparison-span` take a
  verbatim span instead;
* `value` is the plain decimal string (`751766`, not `751,766`);
* `period` is `FY2025` for a fiscal-year flow and `2025-12-31` for a balance
  as at a date;
* metric, basis, unit, scale and sign use the tool-schema enumerations
  (`revenue`, `REPORTED`, `MONETARY`, `MILLION`, `POSITIVE`, ...); currency is
  the abbreviation printed in the document (`RMB`).

Examples (paths abbreviated):

  # An acceptable answer: the total-revenues row of an income statement.
  build_validation_profile.py --document private/.../slice.txt \\
      --source-id tencent-holdings-2025-annual-report \\
      --published-at 2026-04-09 --cutoff 2026-05-01 --question "..." \\
      --current-line "751,766" --current-value 751766 \\
      --current-period FY2025 \\
      --comparison-line "660,257" --comparison-value 660257 \\
      --comparison-period FY2024 \\
      --metric revenue --currency RMB --scale MILLION --output profile.json

  # A question with no admissible evidence in this document.
  build_validation_profile.py --document ... --no-admissible-evidence ...

The written file is validated with the same loader the gate uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from finauditgate.contracts import REVIEWED_PROFILE_MODES
from finauditgate.core.operations import ALLOWLISTED_OPERATIONS, output_unit_for
from finauditgate.adapters.ollama_contract import (
    CURRENCIES,
    METRIC_BASES,
    METRICS,
    SCALES,
    SIGNS,
    UNITS,
)
from finauditgate.core.artifacts import canonical_json_bytes
from finauditgate.core.profiles import (
    PROFILE_SCHEMA_VERSION,
    PrivateDevValidationProfile,
)


def _locate(document: bytes, span: str, label: str) -> tuple[int, int]:
    needle = span.encode("utf-8")
    start = document.find(needle)
    if start < 0:
        raise SystemExit(f"{label}: span not found in the document")
    if document.find(needle, start + 1) >= 0:
        raise SystemExit(f"{label}: span occurs more than once; make it longer")
    return start, start + len(needle)


def _line_range(document: bytes, needle: str, label: str) -> tuple[int, int]:
    """Byte range of the whole line that uniquely contains `needle`."""

    start, end = _locate(document, needle, label)
    line_start = document.rfind(b"\n", 0, start) + 1
    line_end = document.find(b"\n", end)
    if line_end < 0:
        line_end = len(document)
    if not document[line_start:line_end].strip():
        raise SystemExit(f"{label}: the matched line is blank")
    return line_start, line_end


def _line_span(document: bytes, needle: str, label: str) -> str:
    """The stripped document line that uniquely contains `needle`."""

    line_start, line_end = _line_range(document, needle, label)
    return document[line_start:line_end].strip().decode("utf-8")


def _span_argument(
    document: bytes,
    *,
    label: str,
    span: str | None,
    line: str | None,
) -> str:
    if (span is None) == (line is None):
        raise SystemExit(f"{label}: give exactly one of --{label}-span / --{label}-line")
    if span is not None:
        return span
    return _line_span(document, line, label)


def _nth_line_carrying(
    document: bytes,
    value: str,
    occurrence: int,
    label: str,
) -> tuple[int, int]:
    """Byte range of the `occurrence`-th line that prints `value` (1-based)."""

    printed = value if "," in value else f"{int(value):,}" if value.isdigit() else value
    hits: list[tuple[int, int]] = []
    start = 0
    for line in document.split(b"\n"):
        end = start + len(line)
        if printed.encode("utf-8") in line or value.encode("utf-8") in line:
            hits.append((start, end))
        start = end + 1
    if not 1 <= occurrence <= len(hits):
        raise SystemExit(
            f"{label}: value printed on {len(hits)} lines; "
            f"occurrence {occurrence} is out of range"
        )
    return hits[occurrence - 1]


def _evidence(
    document: bytes,
    *,
    evidence_id: str,
    role: str,
    span: str,
    value: str,
    period: str,
    semantics: dict[str, str],
    corroboration_line: str | None = None,
    corroboration_occurrence: int | None = None,
) -> dict[str, object]:
    start, end = _locate(document, span, evidence_id)
    corroboration = None
    if corroboration_occurrence is not None:
        # A figure's second printing is often an unlabelled repeat -- a total
        # restated under its own breakdown -- with no text that distinguishes
        # its line from the first. Naming it by which printing it is, is the
        # only locator that works, and it still resolves to a byte range the
        # gate checks the same way.
        second_start, second_end = _nth_line_carrying(
            document, value, corroboration_occurrence, evidence_id
        )
    elif corroboration_line is not None:
        second_start, second_end = _line_range(
            document, corroboration_line, f"{evidence_id}-corroboration"
        )
    if corroboration_occurrence is not None or corroboration_line is not None:
        corroboration = {
            "byte_start": second_start,
            "byte_end": second_end,
            "span_sha256": hashlib.sha256(
                document[second_start:second_end]
            ).hexdigest(),
        }
    return {
        "evidence_id": evidence_id,
        "role": role,
        "byte_start": start,
        "byte_end": end,
        "span_sha256": hashlib.sha256(document[start:end]).hexdigest(),
        "value": value,
        "normalized_semantics": {**semantics, "fiscal_period": period},
        "corroboration": corroboration,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--document-name")
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--profile-name", default="private-dev-profile/v1")
    parser.add_argument(
        "--current-corroboration-line",
        help=(
            "unique substring of a second line that prints the same current "
            "value, for example the note that breaks down a statement total."
        ),
    )
    parser.add_argument(
        "--comparison-corroboration-line",
        help="the same, for the comparison value.",
    )
    parser.add_argument(
        "--current-corroboration-occurrence",
        type=int,
        help=(
            "which printing of the current value corroborates it, counting "
            "lines that carry it from the top of the document. Use this when "
            "the second printing is an unlabelled repeat that no substring "
            "can name."
        ),
    )
    parser.add_argument(
        "--operation",
        default="growth_rate_percent",
        choices=ALLOWLISTED_OPERATIONS,
        help=(
            "growth_rate_percent answers in PERCENT; absolute_change answers "
            "in the unit of the cited figures."
        ),
    )
    parser.add_argument(
        "--accepted-mode",
        default="PRIVATE_DEV",
        choices=REVIEWED_PROFILE_MODES,
        help=(
            "the split this profile may decide; a task in the other "
            "reviewed mode is refused with MODE_CONFLICT."
        ),
    )
    parser.add_argument("--no-admissible-evidence", action="store_true")
    parser.add_argument("--current-span")
    parser.add_argument(
        "--current-line",
        help="Unique substring of the line carrying the current value.",
    )
    parser.add_argument("--current-value")
    parser.add_argument("--current-period")
    parser.add_argument("--comparison-span")
    parser.add_argument(
        "--comparison-line",
        help="Unique substring of the line carrying the comparison value.",
    )
    parser.add_argument("--comparison-value")
    parser.add_argument("--comparison-period")
    parser.add_argument("--metric", default="revenue", choices=METRICS)
    # A ratio reads two different metrics in one period, so the denominator
    # needs a name of its own. It defaults to the numerator's, which is what
    # every operation that compares one metric across two periods wants.
    parser.add_argument("--comparison-metric", default=None, choices=METRICS)
    parser.add_argument("--metric-basis", default="REPORTED", choices=METRIC_BASES)
    parser.add_argument("--currency", default="USD", choices=CURRENCIES)
    parser.add_argument("--unit", default="MONETARY", choices=UNITS)
    parser.add_argument("--scale", default="MILLION", choices=SCALES)
    parser.add_argument("--sign", default="POSITIVE", choices=SIGNS)
    parser.add_argument("--output", type=Path, help="Default: stdout")
    arguments = parser.parse_args()

    document = arguments.document.read_bytes()
    profile: dict[str, object] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "validation_profile": arguments.profile_name,
        "source_id": arguments.source_id,
        "document_name": arguments.document_name or arguments.document.name,
        "document_sha256": hashlib.sha256(document).hexdigest(),
        "declared_published_at": arguments.published_at,
        "task_cutoff": arguments.cutoff,
        "task_question": arguments.question,
        "accepted_mode": arguments.accepted_mode,
        "accepted_risk_class": "LOW",
    }
    if arguments.no_admissible_evidence:
        profile["evidence_allowlist"] = []
        profile["calculation"] = None
    else:
        required = (
            "current_value",
            "current_period",
            "comparison_value",
            "comparison_period",
        )
        missing = [name for name in required if getattr(arguments, name) is None]
        if missing:
            raise SystemExit(
                "missing: " + ", ".join("--" + name.replace("_", "-") for name in missing)
            )
        current_span = _span_argument(
            document,
            label="current",
            span=arguments.current_span,
            line=arguments.current_line,
        )
        comparison_span = _span_argument(
            document,
            label="comparison",
            span=arguments.comparison_span,
            line=arguments.comparison_line,
        )
        # `sign` records whether a figure is printed in parentheses or with a
        # minus. It is a property of each figure, so it is read from each
        # figure rather than declared once for the pair -- a company that swung
        # from profit to loss prints one of them each way, and `--sign` applied
        # to both could not describe that.
        def _semantics(metric: str, value: str) -> dict[str, str]:
            return {
                "metric": metric,
                "metric_basis": arguments.metric_basis,
                "currency": arguments.currency,
                "unit": arguments.unit,
                "scale": arguments.scale,
                "sign": "NEGATIVE" if value.lstrip().startswith("-") else arguments.sign,
            }

        semantics = _semantics(arguments.metric, arguments.current_value)
        comparison_semantics = _semantics(
            arguments.comparison_metric or arguments.metric, arguments.comparison_value
        )
        profile["evidence_allowlist"] = [
            _evidence(
                document,
                evidence_id="current",
                role="CURRENT",
                span=current_span,
                value=arguments.current_value,
                period=arguments.current_period,
                semantics=semantics,
                corroboration_line=arguments.current_corroboration_line,
                corroboration_occurrence=arguments.current_corroboration_occurrence,
            ),
            _evidence(
                document,
                evidence_id="comparison",
                role="COMPARISON",
                span=comparison_span,
                value=arguments.comparison_value,
                period=arguments.comparison_period,
                semantics=comparison_semantics,
                corroboration_line=arguments.comparison_corroboration_line,
            ),
        ]
        profile["calculation"] = {
            "operation": arguments.operation,
            "operand_ids": ["current", "comparison"],
            "output_unit": output_unit_for(arguments.operation, arguments.unit),
            "quantize": "0.01",
            "decimal_context": {
                "precision": 28,
                "rounding": "ROUND_HALF_EVEN",
                "emin": -999999,
                "emax": 999999,
                "capitals": 1,
                "clamp": 0,
            },
        }

    payload = canonical_json_bytes(profile)
    try:
        PrivateDevValidationProfile.from_bytes(payload)
    except ValueError as exc:
        raise SystemExit(f"profile rejected by the gate loader: {exc}") from exc
    if arguments.output is None:
        sys.stdout.write(payload.decode("utf-8") + "\n")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_bytes(payload)
        print(
            json.dumps(
                {
                    "written": str(arguments.output),
                    "profile_sha256": hashlib.sha256(payload).hexdigest(),
                    "evidence_count": len(profile["evidence_allowlist"]),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
