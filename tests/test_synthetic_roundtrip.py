from dataclasses import replace
from decimal import localcontext
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from finauditgate import AuditTask, Decision, FinAuditGate, FrozenDocumentPackage
from finauditgate.adapters.scripted import (
    CalculationCandidate,
    EvidenceCandidate,
    ScriptedCandidate,
    ScriptedModelAdapter,
)


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "northstar_revenue.txt"
)


def evidence_for(document: bytes, evidence_id: str, record: bytes) -> EvidenceCandidate:
    start = document.index(record)
    return EvidenceCandidate(
        evidence_id=evidence_id,
        byte_start=start,
        byte_end=start + len(record),
        metric="revenue",
        period="FY2024" if evidence_id == "revenue_prior" else "FY2025",
        value="100.00" if evidence_id == "revenue_prior" else "120.00",
        unit="USD_MILLION",
    )


def standard_task(document: bytes, task_id: str) -> AuditTask:
    return AuditTask(
        task_id=task_id,
        question="What was FY2025 revenue growth versus FY2024?",
        cutoff=date(2026, 8, 12),
        document=FrozenDocumentPackage(
            source_id="synthetic-northstar-revenue-v1",
            document_name="northstar_revenue.txt",
            document_bytes=document,
            declared_published_at=date(2026, 8, 12),
        ),
    )


def standard_candidate(document: bytes) -> ScriptedCandidate:
    prior_record = (
        b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
    )
    current_record = (
        b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
    )
    return ScriptedCandidate(
        evidence=(
            evidence_for(document, "revenue_prior", prior_record),
            evidence_for(document, "revenue_current", current_record),
        ),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("revenue_current", "revenue_prior"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )


class SyntheticRoundTripTest(unittest.TestCase):
    def test_unlisted_attempts_file_cannot_reclassify_an_m1_run(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-m1-generation-routing")

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {task.task_id: standard_candidate(document)}
                ),
            ).run(task)
            (
                root
                / "runs"
                / outcome.run_ref.run_id
                / "attempts.json"
            ).write_bytes(b"{}")
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertTrue(replay.consistent)
        self.assertEqual("finauditgate.replay/v1", replay.schema_version)
        self.assertEqual(8, replay.verified_artifact_count)

    def test_candidate_string_subclass_is_normalized_from_canonical_bytes(self) -> None:
        class EquivocatingStr(str):
            def __eq__(self, other: object) -> bool:
                return True

            __hash__ = str.__hash__

        document = FIXTURE_PATH.read_bytes()
        valid = standard_candidate(document)
        candidate = ScriptedCandidate(
            evidence=(
                replace(
                    valid.evidence[0],
                    metric=EquivocatingStr("profit"),
                ),
                replace(
                    valid.evidence[1],
                    metric=EquivocatingStr("profit"),
                ),
            ),
            calculation=valid.calculation,
        )
        task = standard_task(document, "synthetic-candidate-string-subclass")

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.HUMAN_REVIEW, replay.decision)

    def test_one_shot_candidate_is_snapshotted_before_validation(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        valid = standard_candidate(document)
        candidate = ScriptedCandidate(
            evidence=(item for item in valid.evidence),
            calculation=valid.calculation,
        )
        task = standard_task(document, "synthetic-one-shot-candidate")

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            replay = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

        self.assertEqual(Decision.ACCEPT, outcome.decision)
        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ACCEPT, replay.decision)
        self.assertEqual(outcome.answer, replay.answer)

    def test_m1_profile_rejects_each_out_of_profile_task_identity(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        base = standard_task(document, "synthetic-profile-identity")
        cases = {
            "question": replace(base, question="What was profit growth?"),
            "mode": replace(base, mode="PRIVATE_DEV"),
            "source_id": replace(
                base,
                document=replace(base.document, source_id="unrecognized-source"),
            ),
            "document_sha256": replace(
                base,
                document=replace(
                    base.document,
                    document_bytes=document + b"\nsynthetic-extra=true\n",
                ),
            ),
        }

        for field_name, task in cases.items():
            with self.subTest(field_name=field_name):
                candidate = standard_candidate(task.document.document_bytes)
                with tempfile.TemporaryDirectory() as artifact_root:
                    root = Path(artifact_root)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {task.task_id: candidate}
                        ),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(
                        outcome.run_ref
                    )

                self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.HUMAN_REVIEW, replay.decision)

    def test_identical_run_is_idempotent_and_append_only(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-idempotent")
        candidate = standard_candidate(document)

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            gate = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            )
            first = gate.run(task)
            first_snapshot = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            second = gate.run(task)
            second_snapshot = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            self.assertEqual(first.run_ref, second.run_ref)
            self.assertEqual(first_snapshot, second_snapshot)

            outcome_path = (
                root / "runs" / first.run_ref.run_id / "outcome.json"
            )
            outcome_path.write_bytes(b"forged")
            with self.assertRaisesRegex(
                RuntimeError,
                "append-only artifact conflict",
            ):
                gate.run(task)
            self.assertEqual(b"forged", outcome_path.read_bytes())

    def test_non_string_or_blank_lineage_ids_require_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        valid = standard_candidate(document)
        cases = {
            "integer": ScriptedCandidate(
                evidence=(
                    replace(valid.evidence[0], evidence_id=1),
                    replace(valid.evidence[1], evidence_id=2),
                ),
                calculation=replace(
                    valid.calculation,
                    operand_ids=(2, 1),
                ),
            ),
            "blank": ScriptedCandidate(
                evidence=(
                    replace(valid.evidence[0], evidence_id=" "),
                    replace(valid.evidence[1], evidence_id="\t"),
                ),
                calculation=replace(
                    valid.calculation,
                    operand_ids=("\t", " "),
                ),
            ),
        }

        for case_name, candidate in cases.items():
            with self.subTest(case_name=case_name):
                task = standard_task(
                    document,
                    f"synthetic-lineage-{case_name}",
                )
                with tempfile.TemporaryDirectory() as artifact_root:
                    root = Path(artifact_root)
                    outcome = FinAuditGate(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {task.task_id: candidate}
                        ),
                    ).run(task)
                    replay = FinAuditGate(artifact_root=root).replay(
                        outcome.run_ref
                    )

                self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
                self.assertTrue(replay.consistent)
                self.assertEqual(Decision.HUMAN_REVIEW, replay.decision)

    def test_scripted_run_replays_without_a_model(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )
        task = AuditTask(
            task_id="synthetic-revenue-growth",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            gate = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            )

            outcome = gate.run(task)

            self.assertEqual(Decision.ACCEPT, outcome.decision)
            self.assertEqual("20.00", outcome.answer)
            self.assertEqual("PERCENT", outcome.answer_unit)
            self.assertEqual("finauditgate.run/v1", outcome.schema_version)

            offline_gate = FinAuditGate(artifact_root=Path(artifact_root))
            report = offline_gate.replay(outcome.run_ref)

            self.assertTrue(report.consistent)
            self.assertEqual(Decision.ACCEPT, report.decision)
            self.assertEqual("20.00", report.answer)
            self.assertEqual("PERCENT", report.answer_unit)
            self.assertEqual("finauditgate.replay/v1", report.schema_version)
            self.assertEqual(8, report.verified_artifact_count)

    def test_replay_ignores_process_decimal_precision(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-decimal-context",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

            with localcontext() as process_context:
                process_context.prec = 2
                report = FinAuditGate(artifact_root=root).replay(
                    outcome.run_ref
                )

            self.assertTrue(report.consistent)
            self.assertEqual(Decision.ACCEPT, report.decision)
            self.assertEqual("20.00", report.answer)

    def test_candidate_that_disagrees_with_frozen_bytes_requires_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        incorrect_current = evidence_for(
            document,
            "revenue_current",
            current_record,
        )
        incorrect_current = EvidenceCandidate(
            evidence_id=incorrect_current.evidence_id,
            byte_start=incorrect_current.byte_start,
            byte_end=incorrect_current.byte_end,
            metric=incorrect_current.metric,
            period=incorrect_current.period,
            value="999.00",
            unit=incorrect_current.unit,
        )
        task = AuditTask(
            task_id="synthetic-invalid-evidence",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                incorrect_current,
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            gate = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            )

            outcome = gate.run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)
            self.assertEqual(
                ("CANDIDATE_VALIDATION_FAILED",),
                outcome.reason_codes,
            )

            report = FinAuditGate(
                artifact_root=Path(artifact_root)
            ).replay(outcome.run_ref)
            self.assertTrue(report.consistent)
            self.assertEqual(Decision.HUMAN_REVIEW, report.decision)
            self.assertIsNone(report.answer)
            self.assertIsNone(report.answer_unit)

    def test_growth_candidate_with_financial_output_unit_requires_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-wrong-output-unit",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="USD_MILLION",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            gate = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            )

            outcome = gate.run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)
            self.assertEqual(
                ("CANDIDATE_VALIDATION_FAILED",),
                outcome.reason_codes,
            )

    def test_candidate_with_unknown_operand_requires_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-unknown-operand",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "missing_operand"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)
            self.assertEqual(
                ("CANDIDATE_VALIDATION_FAILED",),
                outcome.reason_codes,
            )

    def test_reversed_period_operands_require_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-reversed-periods",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_prior", "revenue_current"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)

    def test_post_cutoff_document_requires_review_and_replays(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = replace(
            standard_task(document, "synthetic-post-cutoff"),
            cutoff=date(2026, 8, 11),
            document=replace(
                standard_task(
                    document,
                    "synthetic-post-cutoff",
                ).document,
                declared_published_at=date(2026, 8, 12),
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {task.task_id: standard_candidate(document)}
                ),
            ).run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)
            self.assertEqual(
                ("CANDIDATE_VALIDATION_FAILED",),
                outcome.reason_codes,
            )

            report = FinAuditGate(artifact_root=root).replay(outcome.run_ref)
            self.assertTrue(report.consistent)
            self.assertEqual(Decision.HUMAN_REVIEW, report.decision)
            self.assertIsNone(report.answer)
            self.assertIsNone(report.answer_unit)

    def test_wrong_metric_candidate_requires_review(self) -> None:
        document = (
            b"issuer=Northstar Components;synthetic=true\n"
            b"metric=profit;period=FY2024;value=100.00;unit=USD_MILLION\n"
            b"metric=profit;period=FY2025;value=150.00;unit=USD_MILLION\n"
        )
        prior_record = (
            b"metric=profit;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=profit;period=FY2025;value=150.00;unit=USD_MILLION"
        )

        def profit_evidence(
            evidence_id: str,
            record: bytes,
            period: str,
            value: str,
        ) -> EvidenceCandidate:
            start = document.index(record)
            return EvidenceCandidate(
                evidence_id=evidence_id,
                byte_start=start,
                byte_end=start + len(record),
                metric="profit",
                period=period,
                value=value,
                unit="USD_MILLION",
            )

        task = replace(
            standard_task(document, "synthetic-wrong-metric"),
            document=replace(
                standard_task(
                    document,
                    "synthetic-wrong-metric",
                ).document,
                document_name="northstar_profit.txt",
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                profit_evidence(
                    "profit_prior", prior_record, "FY2024", "100.00"
                ),
                profit_evidence(
                    "profit_current", current_record, "FY2025", "150.00"
                ),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("profit_current", "profit_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

        self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
        self.assertIsNone(outcome.answer)

    def test_wrong_period_profile_candidate_requires_review(self) -> None:
        document = (
            b"issuer=Northstar Components;synthetic=true\n"
            b"metric=revenue;period=FY2022;value=100.00;unit=USD_MILLION\n"
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION\n"
        )
        prior_record = (
            b"metric=revenue;period=FY2022;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )

        def period_evidence(
            evidence_id: str,
            record: bytes,
            period: str,
            value: str,
        ) -> EvidenceCandidate:
            start = document.index(record)
            return EvidenceCandidate(
                evidence_id=evidence_id,
                byte_start=start,
                byte_end=start + len(record),
                metric="revenue",
                period=period,
                value=value,
                unit="USD_MILLION",
            )

        task = standard_task(document, "synthetic-wrong-period-profile")
        candidate = ScriptedCandidate(
            evidence=(
                period_evidence(
                    "revenue_prior", prior_record, "FY2022", "100.00"
                ),
                period_evidence(
                    "revenue_current", current_record, "FY2025", "120.00"
                ),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

        self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)

    def test_non_financial_input_unit_requires_review(self) -> None:
        document = (
            b"issuer=Northstar Components;synthetic=true\n"
            b"metric=revenue;period=FY2024;value=100.00;unit=EMPLOYEES\n"
            b"metric=revenue;period=FY2025;value=120.00;unit=EMPLOYEES\n"
        )

        def employee_evidence(
            evidence_id: str,
            record: bytes,
            period: str,
            value: str,
        ) -> EvidenceCandidate:
            start = document.index(record)
            return EvidenceCandidate(
                evidence_id=evidence_id,
                byte_start=start,
                byte_end=start + len(record),
                metric="revenue",
                period=period,
                value=value,
                unit="EMPLOYEES",
            )

        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=EMPLOYEES"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=EMPLOYEES"
        )
        task = standard_task(document, "synthetic-non-financial-unit")
        candidate = ScriptedCandidate(
            evidence=(
                employee_evidence(
                    "revenue_prior", prior_record, "FY2024", "100.00"
                ),
                employee_evidence(
                    "revenue_current", current_record, "FY2025", "120.00"
                ),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

        self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)

    def test_material_risk_task_requires_review_in_the_m1_profile(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = replace(
            standard_task(document, "synthetic-material-risk"),
            risk_class="MATERIAL",
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter(
                    {task.task_id: standard_candidate(document)}
                ),
            ).run(task)

        self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)

    def test_candidate_cannot_choose_rounding_quantum(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-untrusted-rounding",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="1E+2",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)

    def test_duplicate_operands_require_review(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-duplicate-operands")
        valid_candidate = standard_candidate(document)
        candidate = ScriptedCandidate(
            evidence=valid_candidate.evidence,
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_current"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            outcome = FinAuditGate(
                artifact_root=Path(artifact_root),
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)

            self.assertEqual(Decision.HUMAN_REVIEW, outcome.decision)
            self.assertIsNone(outcome.answer)

    def test_replay_rejects_forged_outcome_metadata(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-tamper-outcome",
            question="What was FY2025 revenue growth versus FY2024?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-northstar-revenue-v1",
                document_name="northstar_revenue.txt",
                document_bytes=document,
                declared_published_at=date(2026, 8, 12),
            ),
        )
        candidate = ScriptedCandidate(
            evidence=(
                evidence_for(document, "revenue_prior", prior_record),
                evidence_for(document, "revenue_current", current_record),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            run_directory = root / "runs" / outcome.run_ref.run_id
            outcome_path = run_directory / "outcome.json"
            outcome_payload = json.loads(outcome_path.read_bytes())
            outcome_payload["task_id"] = "forged-task"
            outcome_payload["document_sha256"] = "0" * 64
            forged_outcome = json.dumps(
                outcome_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            outcome_path.write_bytes(forged_outcome)

            manifest_path = run_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["artifacts"]["outcome"]["sha256"] = hashlib.sha256(
                forged_outcome
            ).hexdigest()
            manifest_path.write_bytes(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )

            report = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

            self.assertFalse(report.consistent)
            self.assertEqual("OUTCOME_METADATA_MISMATCH", report.reason)

    def test_replay_rejects_unsupported_task_schema(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-schema-tamper")
        candidate = standard_candidate(document)

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            run_directory = root / "runs" / outcome.run_ref.run_id
            task_path = run_directory / "task.json"
            task_payload = json.loads(task_path.read_bytes())
            task_payload["schema_version"] = "finauditgate.task/v999"
            forged_task = json.dumps(
                task_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            task_path.write_bytes(forged_task)

            manifest_path = run_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["artifacts"]["task"]["sha256"] = hashlib.sha256(
                forged_task
            ).hexdigest()
            manifest_path.write_bytes(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )

            report = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

            self.assertFalse(report.consistent)
            self.assertEqual("UNSUPPORTED_TASK_SCHEMA", report.reason)

    def test_replay_fails_closed_for_malformed_manifest(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-malformed-manifest")
        candidate = standard_candidate(document)

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            manifest_path = (
                root / "runs" / outcome.run_ref.run_id / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_bytes())
            manifest["artifacts"]["task"] = []
            manifest_path.write_bytes(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )

            report = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

            self.assertFalse(report.consistent)
            self.assertEqual("MALFORMED_MANIFEST", report.reason)

    def test_replay_rejects_missing_artifact(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        task = standard_task(document, "synthetic-missing-artifact")
        candidate = standard_candidate(document)

        with tempfile.TemporaryDirectory() as artifact_root:
            root = Path(artifact_root)
            outcome = FinAuditGate(
                artifact_root=root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            (
                root / "runs" / outcome.run_ref.run_id / "ledger.json"
            ).unlink()

            report = FinAuditGate(artifact_root=root).replay(outcome.run_ref)

            self.assertFalse(report.consistent)
            self.assertEqual("MISSING_ARTIFACT", report.reason)


if __name__ == "__main__":
    unittest.main()
