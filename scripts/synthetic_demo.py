"""Run and replay the frozen Northstar synthetic profile offline."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from finauditgate import AuditTask, FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters.scripted import (
    CalculationCandidate,
    EvidenceCandidate,
    ScriptedCandidate,
    ScriptedModelAdapter,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "fixtures"
    / "synthetic"
    / "northstar_revenue.txt"
)


def _evidence(
    document: bytes,
    *,
    evidence_id: str,
    record: bytes,
    period: str,
    value: str,
) -> EvidenceCandidate:
    byte_start = document.index(record)
    return EvidenceCandidate(
        evidence_id=evidence_id,
        byte_start=byte_start,
        byte_end=byte_start + len(record),
        metric="revenue",
        period=period,
        value=value,
        unit="USD_MILLION",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the scripted synthetic FinAuditGate profile, then replay it "
            "with a fresh gate that has no model Adapter."
        )
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        type=Path,
        help="Local directory for append-only demo artifacts.",
    )
    arguments = parser.parse_args()

    document = FIXTURE_PATH.read_bytes()
    prior_record = (
        b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
    )
    current_record = (
        b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
    )
    task = AuditTask(
        task_id="synthetic-revenue-growth",
        question="What was FY2025 revenue growth versus FY2024?",
        cutoff=date(2026, 8, 12),
        document=FrozenDocumentPackage(
            source_id="synthetic-northstar-revenue-v1",
            document_name=FIXTURE_PATH.name,
            document_bytes=document,
            declared_published_at=date(2026, 8, 12),
        ),
    )
    candidate = ScriptedCandidate(
        evidence=(
            _evidence(
                document,
                evidence_id="revenue_prior",
                record=prior_record,
                period="FY2024",
                value="100.00",
            ),
            _evidence(
                document,
                evidence_id="revenue_current",
                record=current_record,
                period="FY2025",
                value="120.00",
            ),
        ),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("revenue_current", "revenue_prior"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )
    outcome = FinAuditGate(
        artifact_root=arguments.artifact_root,
        model=ScriptedModelAdapter({task.task_id: candidate}),
    ).run(task)
    replay = FinAuditGate(artifact_root=arguments.artifact_root).replay(
        outcome.run_ref
    )

    payload = {
        "schema_version": "finauditgate.synthetic-demo/v1",
        "outcome": {
            "schema_version": outcome.schema_version,
            "task_id": outcome.task_id,
            "decision": outcome.decision.value,
            "answer": outcome.answer,
            "answer_unit": outcome.answer_unit,
            "run_ref": {"run_id": outcome.run_ref.run_id},
            "document_sha256": outcome.document_sha256,
            "reason_codes": list(outcome.reason_codes),
        },
        "replay": {
            "schema_version": replay.schema_version,
            "run_ref": {"run_id": replay.run_ref.run_id},
            "consistent": replay.consistent,
            "decision": (
                replay.decision.value if replay.decision is not None else None
            ),
            "answer": replay.answer,
            "answer_unit": replay.answer_unit,
            "verified_artifact_count": replay.verified_artifact_count,
            "reason": replay.reason,
        },
    }
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if replay.consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
