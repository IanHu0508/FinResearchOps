"""Run and replay the public synthetic profile offline, with no model.

The scripted Adapter plays the role of the model: it hands the gate a fixed
proposal for the Aurora fixture.  The gate validates it, calculates the growth
rate with Decimal arithmetic, persists the run, and a second gate with no
Adapter at all replays it from the stored artifacts.
"""

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
    REPOSITORY_ROOT / "fixtures" / "synthetic" / "aurora_revenue_growth_m2.txt"
)
QUESTION = "What was Aurora Devices FY2025 revenue growth versus FY2024?"
PRIOR_RECORD = (
    b"metric=Net sales;basis=Reported;period=Year ended 2024-12-31;"
    b"value=125.00;currency=US dollar;unit=Monetary;scale=Millions;"
    b"sign=Positive"
)
CURRENT_RECORD = (
    b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
    b"value=150.00;currency=USD;unit=Currency amount;scale=Million;"
    b"sign=As presented"
)


def _evidence(document: bytes, evidence_id: str, record: bytes) -> EvidenceCandidate:
    values = dict(
        field.split("=", 1) for field in record.decode("utf-8").split(";")
    )
    start = document.index(record)
    return EvidenceCandidate(
        evidence_id=evidence_id,
        byte_start=start,
        byte_end=start + len(record),
        metric=values["metric"],
        metric_basis=values["basis"],
        period=values["period"],
        value=values["value"],
        currency=values["currency"],
        unit=values["unit"],
        scale=values["scale"],
        sign=values["sign"],
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
    task = AuditTask(
        task_id="synthetic-aurora-revenue-growth-v2",
        question=QUESTION,
        cutoff=date(2026, 3, 1),
        document=FrozenDocumentPackage(
            source_id="synthetic-aurora-revenue-growth-v2",
            document_name=FIXTURE_PATH.name,
            document_bytes=document,
            declared_published_at=date(2026, 2, 15),
        ),
    )
    candidate = ScriptedCandidate(
        evidence=(
            _evidence(document, "revenue_prior", PRIOR_RECORD),
            _evidence(document, "revenue_current", CURRENT_RECORD),
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
