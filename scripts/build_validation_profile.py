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
from finauditgate.adapters.ollama_contract import (
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


def _line_span(document: bytes, needle: str, label: str) -> str:
    """The stripped document line that uniquely contains `needle`."""

    start, end = _locate(document, needle, label)
    line_start = document.rfind(b"\n", 0, start) + 1
    line_end = document.find(b"\n", end)
    if line_end < 0:
        line_end = len(document)
    line = document[line_start:line_end].strip()
    if not line:
        raise SystemExit(f"{label}: the matched line is blank")
    return line.decode("utf-8")


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


def _evidence(
    document: bytes,
    *,
    evidence_id: str,
    role: str,
    span: str,
    value: str,
    period: str,
    semantics: dict[str, str],
) -> dict[str, object]:
    start, end = _locate(document, span, evidence_id)
    return {
        "evidence_id": evidence_id,
        "role": role,
        "byte_start": start,
        "byte_end": end,
        "span_sha256": hashlib.sha256(document[start:end]).hexdigest(),
        "value": value,
        "normalized_semantics": {**semantics, "fiscal_period": period},
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
    parser.add_argument("--metric-basis", default="REPORTED", choices=METRIC_BASES)
    parser.add_argument("--currency", default="USD")
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
        semantics = {
            "metric": arguments.metric,
            "metric_basis": arguments.metric_basis,
            "currency": arguments.currency,
            "unit": arguments.unit,
            "scale": arguments.scale,
            "sign": arguments.sign,
        }
        profile["evidence_allowlist"] = [
            _evidence(
                document,
                evidence_id="current",
                role="CURRENT",
                span=current_span,
                value=arguments.current_value,
                period=arguments.current_period,
                semantics=semantics,
            ),
            _evidence(
                document,
                evidence_id="comparison",
                role="COMPARISON",
                span=comparison_span,
                value=arguments.comparison_value,
                period=arguments.comparison_period,
                semantics=semantics,
            ),
        ]
        profile["calculation"] = {
            "operation": "growth_rate_percent",
            "operand_ids": ["current", "comparison"],
            "output_unit": "PERCENT",
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
