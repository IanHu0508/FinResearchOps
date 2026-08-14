from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import date, datetime, timezone
from inspect import signature
import json
from pathlib import Path
import shutil
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from finauditgate import Decision, FrozenDocumentPackage
from finauditgate.adapters.scripted import (
    CalculationCandidate,
    EvidenceCandidate,
    ScriptedCandidate,
    ScriptedModelAdapter,
)
from finauditgate.application import (
    ApplicationError,
    CaseStatus,
    CreateCase,
    ExportChangePacket,
    FinResearchOps,
    ReplayRun,
    ReviewAction,
    RunAnalysis,
    SubmitReview,
)
from finauditgate.core.artifacts import canonical_json_bytes, sha256_hex
import finauditgate.application.module as application_module


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "northstar_revenue.txt"
)
AURORA_FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "aurora_revenue_growth_m2.txt"
)
SCHEMA_DIR = Path(__file__).parents[1] / "schemas"


def northstar_document() -> FrozenDocumentPackage:
    return FrozenDocumentPackage(
        source_id="synthetic-northstar-revenue-v1",
        document_name="northstar_revenue.txt",
        document_bytes=FIXTURE_PATH.read_bytes(),
        declared_published_at=date(2026, 8, 12),
    )


def northstar_candidate() -> ScriptedCandidate:
    document = FIXTURE_PATH.read_bytes()
    prior = b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
    current = b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"

    def evidence(
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

    return ScriptedCandidate(
        evidence=(
            evidence("revenue_prior", prior, "FY2024", "100.00"),
            evidence("revenue_current", current, "FY2025", "120.00"),
        ),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("revenue_current", "revenue_prior"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )


def invalid_value_candidate() -> ScriptedCandidate:
    valid = northstar_candidate()
    current = valid.evidence[1]
    return ScriptedCandidate(
        evidence=(
            valid.evidence[0],
            EvidenceCandidate(
                evidence_id=current.evidence_id,
                byte_start=current.byte_start,
                byte_end=current.byte_end,
                metric=current.metric,
                period=current.period,
                value="999.00",
                unit=current.unit,
            ),
        ),
        calculation=valid.calculation,
    )


def aurora_document() -> FrozenDocumentPackage:
    return FrozenDocumentPackage(
        source_id="synthetic-aurora-revenue-growth-v2",
        document_name="aurora_revenue_growth_m2.txt",
        document_bytes=AURORA_FIXTURE_PATH.read_bytes(),
        declared_published_at=date(2026, 2, 15),
    )


def aurora_candidate() -> ScriptedCandidate:
    document = AURORA_FIXTURE_PATH.read_bytes()
    records = (
        (
            "revenue_prior",
            b"metric=Net sales;basis=Reported;period=Year ended 2024-12-31;"
            b"value=125.00;currency=US dollar;unit=Monetary;scale=Millions;"
            b"sign=Positive",
        ),
        (
            "revenue_current",
            b"metric=Total revenue;basis=IFRS reported;period=FY 2025;"
            b"value=150.00;currency=USD;unit=Currency amount;scale=Million;"
            b"sign=As presented",
        ),
    )
    evidence_items = []
    for evidence_id, record in records:
        values = dict(
            field.split("=", 1)
            for field in record.decode("utf-8").split(";")
        )
        start = document.index(record)
        evidence_items.append(
            EvidenceCandidate(
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
        )
    return ScriptedCandidate(
        evidence=tuple(evidence_items),
        calculation=CalculationCandidate(
            operation="growth_rate_percent",
            operand_ids=("revenue_current", "revenue_prior"),
            output_unit="PERCENT",
            quantize="0.01",
        ),
    )


class M2ApplicationTest(unittest.TestCase):
    def test_formal_m2_application_schemas_are_closed_and_versioned(self) -> None:
        expected = {
            "case-journal-head.v1.schema.json": (
                "urn:finresearchops:schema:case-journal-head:v1",
                "finresearchops.case-journal-head/v1",
            ),
            "case-record.v1.schema.json": (
                "urn:finresearchops:schema:case-record:v1",
                "finresearchops.case-record/v1",
            ),
            "case-transaction.v1.schema.json": (
                "urn:finresearchops:schema:case-transaction:v1",
                "finresearchops.case-transaction/v1",
            ),
            "case-transaction-commit.v1.schema.json": (
                "urn:finresearchops:schema:case-transaction-commit:v1",
                "finresearchops.case-transaction-commit/v1",
            ),
            "workpaper.v1.schema.json": (
                "urn:finresearchops:schema:workpaper:v1",
                "finresearchops.workpaper/v1",
            ),
            "review-record.v1.schema.json": (
                "urn:finresearchops:schema:review-record:v1",
                "finresearchops.review-record/v1",
            ),
            "research-change-packet.v1.schema.json": (
                "urn:finresearchops:schema:research-change-packet:v1",
                "finresearchops.research-change-packet/v1",
            ),
            "replay-record.v1.schema.json": (
                "urn:finresearchops:schema:replay-record:v1",
                "finresearchops.replay-record/v1",
            ),
        }
        for filename, (schema_id, schema_version) in expected.items():
            with self.subTest(filename=filename):
                payload = json.loads((SCHEMA_DIR / filename).read_bytes())
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema",
                    payload["$schema"],
                )
                self.assertEqual(schema_id, payload["$id"])
                self.assertEqual("object", payload["type"])
                self.assertFalse(payload["additionalProperties"])
                self.assertEqual(
                    schema_version,
                    payload["properties"]["schema_version"]["const"],
                )
                self.assertEqual(
                    set(payload["required"]),
                    set(payload["properties"]),
                )
        packet = json.loads(
            (SCHEMA_DIR / "research-change-packet.v1.schema.json").read_bytes()
        )
        self.assertTrue(packet["properties"]["proposal_only"]["const"])
        self.assertNotIn("formal_research_state", packet["properties"])
        replay_record = json.loads(
            (SCHEMA_DIR / "replay-record.v1.schema.json").read_bytes()
        )
        self.assertEqual(
            {
                "urn:finauditgate:schema:replay-report:v1",
                "urn:finauditgate:schema:replay-report:v2",
            },
            {
                option["$ref"]
                for option in replay_record["properties"]["report"]["anyOf"]
            },
        )

    def test_application_interface_and_commands_are_closed(self) -> None:
        self.assertEqual(
            {"handle", "read_case"},
            {
                name
                for name, value in vars(FinResearchOps).items()
                if callable(value) and not name.startswith("_")
            },
        )
        self.assertEqual(
            ("self", "command"),
            tuple(signature(FinResearchOps.handle).parameters),
        )
        self.assertEqual(
            ("self", "case_ref"),
            tuple(signature(FinResearchOps.read_case).parameters),
        )
        self.assertEqual(
            (
                "question",
                "cutoff",
                "document",
                "answer_contract",
                "risk_class",
                "mode",
            ),
            tuple(field.name for field in fields(CreateCase)),
        )
        self.assertNotIn("decision", {field.name for field in fields(CreateCase)})
        self.assertNotIn("answer", {field.name for field in fields(CreateCase)})

    def test_create_run_and_read_never_turn_machine_accept_into_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application = FinResearchOps(
                artifact_root=Path(temporary_directory),
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            command = CreateCase(
                question="What was FY2025 revenue growth versus FY2024?",
                cutoff=date(2026, 8, 12),
                document=northstar_document(),
            )

            created = application.handle(command)
            created_again = application.handle(command)
            before_run = application.read_case(created.case_ref)

            self.assertEqual(created.case_ref, created_again.case_ref)
            self.assertEqual(CaseStatus.CREATED, created.status)
            self.assertEqual(CaseStatus.CREATED, before_run.status)
            self.assertEqual((), before_run.run_refs)

            analysis = application.handle(RunAnalysis(created.case_ref))
            view = application.read_case(created.case_ref)

            self.assertEqual(CaseStatus.AWAITING_REVIEW, analysis.status)
            self.assertEqual(CaseStatus.AWAITING_REVIEW, view.status)
            self.assertEqual(Decision.ACCEPT, analysis.machine_decision)
            self.assertEqual(Decision.ACCEPT, view.latest_machine_decision)
            self.assertEqual("20.00", view.workpapers[0].answer)
            self.assertEqual("PERCENT", view.workpapers[0].answer_unit)
            self.assertIsNone(view.workpapers[0].attempts_artifact_ref)
            self.assertEqual(
                view.workpapers[0].candidate_artifact_ref,
                view.workpapers[0].trace_summary_ref,
            )
            self.assertEqual(
                "SCRIPTED_CANDIDATE_RECORDED",
                view.workpapers[0].trace_summary_status,
            )
            self.assertEqual((analysis.run_ref,), view.run_refs)
            self.assertEqual((), view.reviews)
            self.assertEqual((), view.packets)

    def test_approved_case_exports_one_proposal_and_replays_after_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    12,
                    1,
                    tzinfo=timezone.utc,
                ),
            )
            created = application.handle(
                CreateCase(
                    question=(
                        "What was FY2025 revenue growth versus FY2024?"
                    ),
                    cutoff=date(2026, 8, 12),
                    document=northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))

            reviewed = application.handle(
                SubmitReview(
                    case_ref=created.case_ref,
                    run_ref=analysis.run_ref,
                    action=ReviewAction.APPROVE,
                    reason="Evidence and deterministic calculation checked.",
                )
            )
            packet = application.handle(
                ExportChangePacket(created.case_ref, analysis.run_ref)
            )
            packet_again = application.handle(
                ExportChangePacket(created.case_ref, analysis.run_ref)
            )

            reopened = FinResearchOps(artifact_root=root)
            replay = reopened.handle(ReplayRun(analysis.run_ref))
            replay_again = reopened.handle(ReplayRun(analysis.run_ref))
            view = reopened.read_case(created.case_ref)

            self.assertEqual(CaseStatus.APPROVED, reviewed.status)
            self.assertEqual(CaseStatus.APPROVED, view.status)
            self.assertEqual(ReviewAction.APPROVE, view.reviews[0].action)
            self.assertEqual(analysis.run_ref, view.reviews[0].run_ref)
            self.assertEqual(packet.packet_ref, packet_again.packet_ref)
            self.assertEqual(replay.replay_ref, replay_again.replay_ref)
            self.assertTrue(view.packets[0].proposal_only)
            self.assertEqual(2, len(view.packets[0].verified_facts))
            self.assertEqual("20.00", view.packets[0].calculations[0].result)
            self.assertEqual(analysis.run_ref, view.packets[0].run_ref)
            self.assertEqual(view.reviews[0].review_ref, view.packets[0].review_ref)
            self.assertEqual(Decision.ACCEPT, view.replays[0].report.decision)
            self.assertTrue(view.replays[0].report.consistent)

    def test_non_accept_cannot_be_approved_or_exported_and_return_can_rerun(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application = FinResearchOps(
                artifact_root=Path(temporary_directory),
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            invalid_value_candidate()
                        ),
                        "synthetic-northstar-revenue-v1:analysis-2": (
                            northstar_candidate()
                        ),
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    12,
                    2,
                    tzinfo=timezone.utc,
                ),
            )
            created = application.handle(
                CreateCase(
                    question=(
                        "What was FY2025 revenue growth versus FY2024?"
                    ),
                    cutoff=date(2026, 8, 12),
                    document=northstar_document(),
                )
            )
            first = application.handle(RunAnalysis(created.case_ref))

            with self.assertRaises(ApplicationError) as approval_error:
                application.handle(
                    SubmitReview(
                        created.case_ref,
                        first.run_ref,
                        ReviewAction.APPROVE,
                        "Unsafe manual override attempt.",
                    )
                )
            self.assertEqual(
                "MACHINE_DECISION_NOT_APPROVABLE",
                approval_error.exception.code,
            )
            with self.assertRaises(ApplicationError) as export_error:
                application.handle(
                    ExportChangePacket(created.case_ref, first.run_ref)
                )
            self.assertEqual(
                "CASE_NOT_ELIGIBLE_FOR_EXPORT",
                export_error.exception.code,
            )
            pending = application.read_case(created.case_ref)
            self.assertEqual(CaseStatus.AWAITING_REVIEW, pending.status)
            self.assertEqual(Decision.HUMAN_REVIEW, pending.latest_machine_decision)
            self.assertTrue(pending.latest_gate_reason_codes)
            self.assertEqual((), pending.reviews)

            returned = application.handle(
                SubmitReview(
                    created.case_ref,
                    first.run_ref,
                    ReviewAction.RETURN,
                    "Correct the proposed value.",
                )
            )
            self.assertEqual(CaseStatus.RETURNED, returned.status)
            second = application.handle(RunAnalysis(created.case_ref))
            self.assertEqual(Decision.ACCEPT, second.machine_decision)
            rejected = application.handle(
                SubmitReview(
                    created.case_ref,
                    second.run_ref,
                    ReviewAction.REJECT,
                    "Do not advance this synthetic proposal.",
                )
            )
            view = application.read_case(created.case_ref)

            self.assertEqual(CaseStatus.REJECTED, rejected.status)
            self.assertEqual(CaseStatus.REJECTED, view.status)
            self.assertEqual((first.run_ref, second.run_ref), view.run_refs)
            self.assertEqual(
                (ReviewAction.RETURN, ReviewAction.REJECT),
                tuple(record.action for record in view.reviews),
            )
            with self.assertRaises(ApplicationError):
                application.handle(
                    ExportChangePacket(created.case_ref, second.run_ref)
                )

    def test_closed_commands_and_case_run_ownership_fail_without_state_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application = FinResearchOps(
                artifact_root=Path(temporary_directory),
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    12,
                    3,
                    tzinfo=timezone.utc,
                ),
            )
            first_case = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            second_case = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 13),
                    northstar_document(),
                )
            )
            first_run = application.handle(RunAnalysis(first_case.case_ref))
            second_run = application.handle(RunAnalysis(second_case.case_ref))

            class FakeCommand:
                case_ref = first_case.case_ref
                decision = "ACCEPT"

            with self.assertRaises(ApplicationError) as command_error:
                application.handle(FakeCommand())
            self.assertEqual("UNSUPPORTED_COMMAND", command_error.exception.code)
            with self.assertRaises(ApplicationError) as pending_error:
                application.handle(RunAnalysis(first_case.case_ref))
            self.assertEqual(
                "ANALYSIS_NOT_ALLOWED_IN_CURRENT_STATE",
                pending_error.exception.code,
            )
            with self.assertRaises(ApplicationError) as cross_case_error:
                application.handle(
                    SubmitReview(
                        first_case.case_ref,
                        second_run.run_ref,
                        ReviewAction.APPROVE,
                        "Wrong Case and RunRef pairing.",
                    )
                )
            self.assertEqual(
                "REVIEW_RUN_IS_NOT_CURRENT",
                cross_case_error.exception.code,
            )
            with self.assertRaises(ApplicationError) as missing_error:
                application.handle(RunAnalysis("case-" + "0" * 64))
            self.assertEqual("CASE_NOT_FOUND", missing_error.exception.code)
            self.assertFalse(
                (
                    Path(temporary_directory)
                    / "application"
                    / "locks"
                    / f"case-{'0' * 64}.lock"
                ).exists()
            )

            reviewed = application.handle(
                SubmitReview(
                    first_case.case_ref,
                    first_run.run_ref,
                    ReviewAction.APPROVE,
                    "Approve only the correctly linked run.",
                )
            )
            with self.assertRaises(ApplicationError) as duplicate_error:
                application.handle(
                    SubmitReview(
                        first_case.case_ref,
                        first_run.run_ref,
                        ReviewAction.REJECT,
                        "A terminal Case cannot be reviewed again.",
                    )
                )
            self.assertEqual(
                "REVIEW_NOT_ALLOWED_IN_CURRENT_STATE",
                duplicate_error.exception.code,
            )
            with self.assertRaises(ApplicationError) as stale_export_error:
                application.handle(
                    ExportChangePacket(
                        first_case.case_ref,
                        second_run.run_ref,
                    )
                )
            self.assertEqual(
                "EXPORT_RUN_IS_NOT_CURRENT",
                stale_export_error.exception.code,
            )
            first_view = application.read_case(first_case.case_ref)
            second_view = application.read_case(second_case.case_ref)
            self.assertEqual(1, len(first_view.reviews))
            self.assertEqual(CaseStatus.APPROVED, first_view.status)
            self.assertEqual(CaseStatus.AWAITING_REVIEW, second_view.status)
            self.assertEqual((), second_view.reviews)

    def test_tampered_application_or_core_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            workpaper_path = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "workpapers"
                / f"{analysis.workpaper_ref}.json"
            )
            original_workpaper = workpaper_path.read_bytes()
            workpaper_path.write_bytes(b"forged")
            with self.assertRaises(ApplicationError):
                application.read_case(created.case_ref)
            workpaper_path.write_bytes(original_workpaper)

            outcome_path = (
                root
                / "core"
                / "runs"
                / analysis.run_ref.run_id
                / "outcome.json"
            )
            original_outcome = outcome_path.read_bytes()
            outcome_path.write_bytes(b"forged")
            with self.assertRaises(ApplicationError):
                application.handle(ReplayRun(analysis.run_ref))
            outcome_path.write_bytes(original_outcome)

            restored = application.read_case(created.case_ref)
            self.assertEqual(CaseStatus.AWAITING_REVIEW, restored.status)
            self.assertEqual((), restored.replays)

            workpaper_path.unlink()
            with self.assertRaises(ApplicationError) as missing_workpaper:
                application.read_case(created.case_ref)
            self.assertIn(
                missing_workpaper.exception.code,
                {
                    "ARTIFACT_READ_FAILED",
                    "CASE_TRANSACTION_COMMIT_INVALID",
                },
            )

    def test_content_addressed_workpaper_cannot_replace_core_reason_codes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-aurora-revenue-growth-v2": (
                            aurora_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    question=(
                        "What was Aurora Devices FY2025 revenue growth "
                        "versus FY2024?"
                    ),
                    cutoff=date(2026, 2, 14),
                    document=aurora_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            workpaper_path = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "workpapers"
                / f"{analysis.workpaper_ref}.json"
            )
            payload = json.loads(workpaper_path.read_bytes())
            payload["gate_reason_codes"] = ["FORGED_REASON"]
            body = {
                key: value
                for key, value in payload.items()
                if key != "workpaper_ref"
            }
            forged_ref = "workpaper-" + sha256_hex(
                canonical_json_bytes(body)
            )
            payload["workpaper_ref"] = forged_ref
            (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "workpapers"
                / f"{forged_ref}.json"
            ).write_bytes(canonical_json_bytes(payload))
            event_path = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "events"
                / "00000001.json"
            )
            event = json.loads(event_path.read_bytes())
            event["workpaper_ref"] = forged_ref
            event_path.write_bytes(canonical_json_bytes(event))

            with self.assertRaises(ApplicationError) as error:
                application.read_case(created.case_ref)
            self.assertIn(
                error.exception.code,
                {
                    "WORKPAPER_CORE_MISMATCH",
                    "CASE_TRANSACTION_PUBLISHED_CONTENT_INVALID",
                },
            )

    def test_analysis_adapter_failure_stays_inside_application_error_family(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application = FinResearchOps(
                artifact_root=Path(temporary_directory),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )

            with self.assertRaises(ApplicationError) as error:
                application.handle(RunAnalysis(created.case_ref))
            self.assertEqual(
                "ANALYSIS_EXECUTION_FAILED",
                error.exception.code,
            )
            self.assertEqual(
                CaseStatus.CREATED,
                application.read_case(created.case_ref).status,
            )

    def test_deleted_tail_review_or_analysis_event_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    12,
                    5,
                    tzinfo=timezone.utc,
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            reviewed = application.handle(
                SubmitReview(
                    created.case_ref,
                    analysis.run_ref,
                    ReviewAction.APPROVE,
                    "Append-only approval.",
                )
            )
            event_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "events"
            )
            (event_directory / "00000002.json").unlink()
            (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "reviews"
                / f"{reviewed.review_ref}.json"
            ).unlink()

            with self.assertRaises(ApplicationError) as review_tail_error:
                application.read_case(created.case_ref)
            self.assertIn(
                review_tail_error.exception.code,
                {
                    "CASE_JOURNAL_HEAD_MISMATCH",
                    "CASE_TRANSACTION_COMMIT_INVALID",
                },
            )
            with self.assertRaises(ApplicationError):
                application.handle(
                    SubmitReview(
                        created.case_ref,
                        analysis.run_ref,
                        ReviewAction.RETURN,
                        "Must not replace the deleted approval event.",
                    )
                )

            workpaper_ref = analysis.workpaper_ref
            (event_directory / "00000001.json").unlink()
            (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "workpapers"
                / f"{workpaper_ref}.json"
            ).unlink()
            with self.assertRaises(ApplicationError) as analysis_tail_error:
                application.read_case(created.case_ref)
            self.assertIn(
                analysis_tail_error.exception.code,
                {
                    "CASE_JOURNAL_HEAD_MISMATCH",
                    "CASE_TRANSACTION_COMMIT_INVALID",
                },
            )

    def test_foreign_case_workpaper_in_case_directory_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            case_a = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            case_b = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 13),
                    northstar_document(),
                )
            )
            analysis_b = application.handle(RunAnalysis(case_b.case_ref))
            source = (
                root
                / "application"
                / "cases"
                / case_b.case_ref
                / "workpapers"
                / f"{analysis_b.workpaper_ref}.json"
            )
            destination = (
                root
                / "application"
                / "cases"
                / case_a.case_ref
                / "workpapers"
                / source.name
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

            with self.assertRaises(ApplicationError) as error:
                application.read_case(case_a.case_ref)

        self.assertEqual(
            "CASE_ARTIFACT_MEMBERSHIP_INVALID",
            error.exception.code,
        )

    def test_analysis_recovers_after_journal_head_publish_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )

            with patch(
                "finauditgate.application.module.os.replace",
                side_effect=OSError("injected journal-head failure"),
            ):
                with self.assertRaises(ApplicationError) as error:
                    application.handle(RunAnalysis(created.case_ref))
            self.assertEqual("APPLICATION_STORAGE_FAILED", error.exception.code)

            reopened = FinResearchOps(artifact_root=root).read_case(
                created.case_ref
            )
            self.assertEqual(CaseStatus.CREATED, reopened.status)
            reopened_application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            reopened_application.handle(RunAnalysis(created.case_ref))
            reopened = reopened_application.read_case(created.case_ref)

        self.assertEqual(CaseStatus.AWAITING_REVIEW, reopened.status)
        self.assertEqual(1, len(reopened.run_refs))
        self.assertEqual(1, len(reopened.workpapers))

    def test_analysis_transition_recovers_at_every_publication_boundary(
        self,
    ) -> None:
        boundaries = ("intent", "workpaper", "event", "head", "commit")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    application = FinResearchOps(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {
                                "synthetic-northstar-revenue-v1": (
                                    northstar_candidate()
                                )
                            }
                        ),
                    )
                    created = application.handle(
                        CreateCase(
                            "What was FY2025 revenue growth versus FY2024?",
                            date(2026, 8, 12),
                            northstar_document(),
                        )
                    )
                    injected = False
                    original_write_once = application_module.write_once

                    def fail_selected_write(path: Path, payload: bytes) -> None:
                        nonlocal injected
                        matches = {
                            "intent": path.name.endswith(".intent.json"),
                            "workpaper": path.parent.name == "workpapers",
                            "event": path.parent.name == "events",
                            "commit": path.name.endswith(".commit.json"),
                            "head": False,
                        }[boundary]
                        if matches and not injected:
                            injected = True
                            raise OSError(f"injected {boundary} failure")
                        original_write_once(path, payload)

                    if boundary == "head":
                        context = patch(
                            "finauditgate.application.module.os.replace",
                            side_effect=OSError("injected head failure"),
                        )
                    else:
                        context = patch(
                            "finauditgate.application.module.write_once",
                            side_effect=fail_selected_write,
                        )
                    with context:
                        with self.assertRaises(ApplicationError) as error:
                            application.handle(RunAnalysis(created.case_ref))
                    self.assertEqual(
                        "APPLICATION_STORAGE_FAILED",
                        error.exception.code,
                    )

                    reopened_application = FinResearchOps(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {
                                "synthetic-northstar-revenue-v1": (
                                    northstar_candidate()
                                )
                            }
                        ),
                    )
                    reopened = reopened_application.read_case(created.case_ref)
                    if boundary != "commit":
                        self.assertEqual(CaseStatus.CREATED, reopened.status)
                        reopened_application.handle(
                            RunAnalysis(created.case_ref)
                        )
                        reopened = reopened_application.read_case(
                            created.case_ref
                        )
                    self.assertEqual(
                        CaseStatus.AWAITING_REVIEW,
                        reopened.status,
                    )
                    self.assertEqual(1, len(reopened.run_refs))
                    self.assertEqual(1, len(reopened.workpapers))

    def test_review_transition_recovers_at_every_publication_boundary(
        self,
    ) -> None:
        boundaries = ("intent", "review", "event", "head", "commit")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    clock = lambda: datetime(
                        2026,
                        8,
                        13,
                        13,
                        0,
                        tzinfo=timezone.utc,
                    )
                    application = FinResearchOps(
                        artifact_root=root,
                        model=ScriptedModelAdapter(
                            {
                                "synthetic-northstar-revenue-v1": (
                                    northstar_candidate()
                                )
                            }
                        ),
                        clock=clock,
                    )
                    created = application.handle(
                        CreateCase(
                            "What was FY2025 revenue growth versus FY2024?",
                            date(2026, 8, 12),
                            northstar_document(),
                        )
                    )
                    analysis = application.handle(
                        RunAnalysis(created.case_ref)
                    )
                    command = SubmitReview(
                        created.case_ref,
                        analysis.run_ref,
                        ReviewAction.APPROVE,
                        "Fault-boundary approval.",
                    )
                    injected = False
                    original_write_once = application_module.write_once

                    def fail_selected_write(path: Path, payload: bytes) -> None:
                        nonlocal injected
                        matches = {
                            "intent": path.name.endswith(".intent.json"),
                            "review": path.parent.name == "reviews",
                            "event": path.parent.name == "events",
                            "commit": path.name.endswith(".commit.json"),
                            "head": False,
                        }[boundary]
                        if matches and not injected:
                            injected = True
                            raise OSError(f"injected {boundary} failure")
                        original_write_once(path, payload)

                    if boundary == "head":
                        context = patch(
                            "finauditgate.application.module.os.replace",
                            side_effect=OSError("injected head failure"),
                        )
                    else:
                        context = patch(
                            "finauditgate.application.module.write_once",
                            side_effect=fail_selected_write,
                        )
                    with context:
                        with self.assertRaises(ApplicationError) as error:
                            application.handle(command)
                    self.assertEqual(
                        "APPLICATION_STORAGE_FAILED",
                        error.exception.code,
                    )

                    reopened_application = FinResearchOps(
                        artifact_root=root,
                        clock=clock,
                    )
                    reopened = reopened_application.read_case(created.case_ref)
                    if boundary != "commit":
                        self.assertEqual(
                            CaseStatus.AWAITING_REVIEW,
                            reopened.status,
                        )
                        reopened_application.handle(command)
                        reopened = reopened_application.read_case(
                            created.case_ref
                        )
                    self.assertEqual(CaseStatus.APPROVED, reopened.status)
                    self.assertEqual(1, len(reopened.reviews))

    def test_foreign_case_review_in_case_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    13,
                    5,
                    tzinfo=timezone.utc,
                ),
            )
            case_a = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            case_b = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 13),
                    northstar_document(),
                )
            )
            analysis_b = application.handle(RunAnalysis(case_b.case_ref))
            review_b = application.handle(
                SubmitReview(
                    case_b.case_ref,
                    analysis_b.run_ref,
                    ReviewAction.APPROVE,
                    "Foreign review source.",
                )
            )
            source = (
                root
                / "application"
                / "cases"
                / case_b.case_ref
                / "reviews"
                / f"{review_b.review_ref}.json"
            )
            destination = (
                root
                / "application"
                / "cases"
                / case_a.case_ref
                / "reviews"
                / source.name
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

            with self.assertRaises(ApplicationError) as error:
                application.read_case(case_a.case_ref)

        self.assertEqual(
            "CASE_ARTIFACT_MEMBERSHIP_INVALID",
            error.exception.code,
        )

    def test_tampered_case_transaction_intent_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            original_write_once = application_module.write_once

            def fail_workpaper(path: Path, payload: bytes) -> None:
                if path.parent.name == "workpapers":
                    raise OSError("leave durable intent pending")
                original_write_once(path, payload)

            with patch(
                "finauditgate.application.module.write_once",
                side_effect=fail_workpaper,
            ):
                with self.assertRaises(ApplicationError):
                    application.handle(RunAnalysis(created.case_ref))
            transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            intent_path = next(transaction_directory.glob("*.intent.json"))
            payload = json.loads(intent_path.read_bytes())
            payload["material_payload"]["case_ref"] = (
                "case-" + "0" * 64
            )
            intent_path.write_bytes(canonical_json_bytes(payload))

            with self.assertRaises(ApplicationError) as error:
                FinResearchOps(artifact_root=root).read_case(created.case_ref)

        self.assertIn(
            error.exception.code,
            {
                "CASE_TRANSACTION_IDENTITY_INVALID",
                "CASE_ARTIFACT_MEMBERSHIP_INVALID",
            },
        )

    def test_committed_transaction_cannot_recreate_deleted_material(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            workpaper_path = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "workpapers"
                / f"{analysis.workpaper_ref}.json"
            )
            workpaper_path.unlink()

            with self.assertRaises(ApplicationError):
                FinResearchOps(artifact_root=root).read_case(created.case_ref)

        self.assertFalse(workpaper_path.exists())

    def test_read_does_not_authorize_an_injected_pending_review_intent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "original"
            clone = Path(temporary_directory) / "clone"
            clock = lambda: datetime(
                2026,
                8,
                13,
                13,
                15,
                tzinfo=timezone.utc,
            )
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=clock,
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            shutil.copytree(root, clone)
            clone_application = FinResearchOps(
                artifact_root=clone,
                clock=clock,
            )
            clone_application.handle(
                SubmitReview(
                    created.case_ref,
                    analysis.run_ref,
                    ReviewAction.APPROVE,
                    "Intent must not be authority.",
                )
            )
            clone_transaction_directory = (
                clone
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            injected_intent = next(
                path
                for path in clone_transaction_directory.glob("*.intent.json")
                if json.loads(path.read_bytes())["sequence"] == 2
            )
            original_transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            destination = original_transaction_directory / injected_intent.name
            destination.write_bytes(injected_intent.read_bytes())

            reopened = FinResearchOps(artifact_root=root).read_case(
                created.case_ref
            )

            self.assertEqual(CaseStatus.AWAITING_REVIEW, reopened.status)
            self.assertEqual((), reopened.reviews)
            self.assertFalse(
                any(
                    json.loads(path.read_bytes()).get("sequence") == 2
                    for path in original_transaction_directory.glob(
                        "*.commit.json"
                    )
                )
            )
            self.assertEqual(
                ["00000001.json"],
                [
                    path.name
                    for path in (
                        root
                        / "application"
                        / "cases"
                        / created.case_ref
                        / "events"
                    ).glob("*.json")
                ],
            )

    def test_commit_marker_without_published_transition_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "original"
            clone = Path(temporary_directory) / "clone"
            clock = lambda: datetime(
                2026,
                8,
                13,
                13,
                20,
                tzinfo=timezone.utc,
            )
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=clock,
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            shutil.copytree(root, clone)
            FinResearchOps(artifact_root=clone, clock=clock).handle(
                SubmitReview(
                    created.case_ref,
                    analysis.run_ref,
                    ReviewAction.APPROVE,
                    "Detached commit must fail.",
                )
            )
            clone_transaction_directory = (
                clone
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            sequence_two_intent = next(
                path
                for path in clone_transaction_directory.glob("*.intent.json")
                if json.loads(path.read_bytes())["sequence"] == 2
            )
            transaction_ref = json.loads(
                sequence_two_intent.read_bytes()
            )["transaction_ref"]
            sequence_two_commit = (
                clone_transaction_directory
                / f"{transaction_ref}.commit.json"
            )
            original_transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            for source in (sequence_two_intent, sequence_two_commit):
                (original_transaction_directory / source.name).write_bytes(
                    source.read_bytes()
                )

            with self.assertRaises(ApplicationError) as error:
                FinResearchOps(artifact_root=root).read_case(created.case_ref)

        self.assertEqual(
            "CASE_TRANSACTION_COMMIT_INVALID",
            error.exception.code,
        )

    def test_pending_review_requires_the_same_public_command_to_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_clock = lambda: datetime(
                2026, 8, 13, 13, 30, tzinfo=timezone.utc
            )
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=first_clock,
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            approve = SubmitReview(
                created.case_ref,
                analysis.run_ref,
                ReviewAction.APPROVE,
                "Resume exactly this approval.",
            )
            original_write_once = application_module.write_once

            def fail_event(path: Path, payload: bytes) -> None:
                if path.parent.name == "events":
                    raise OSError("leave pending review")
                original_write_once(path, payload)

            with patch(
                "finauditgate.application.module.write_once",
                side_effect=fail_event,
            ):
                with self.assertRaises(ApplicationError):
                    application.handle(approve)

            reopened_application = FinResearchOps(
                artifact_root=root,
                clock=lambda: datetime(
                    2026, 8, 13, 15, 45, tzinfo=timezone.utc
                ),
            )
            pending_view = reopened_application.read_case(created.case_ref)
            with self.assertRaises(ApplicationError) as conflict:
                reopened_application.handle(
                    SubmitReview(
                        created.case_ref,
                        analysis.run_ref,
                        ReviewAction.RETURN,
                        "A different command cannot take over.",
                    )
                )
            resumed = reopened_application.handle(approve)
            final_view = reopened_application.read_case(created.case_ref)

        self.assertEqual(CaseStatus.AWAITING_REVIEW, pending_view.status)
        self.assertEqual(
            "CASE_TRANSACTION_PENDING_CONFLICT",
            conflict.exception.code,
        )
        self.assertEqual(CaseStatus.APPROVED, resumed.status)
        self.assertEqual(CaseStatus.APPROVED, final_view.status)
        self.assertEqual(first_clock(), final_view.reviews[-1].recorded_at)

    def test_domain_invalid_pending_review_never_advances_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "original"
            clone = Path(temporary_directory) / "clone"
            clock = lambda: datetime(
                2026, 8, 13, 13, 40, tzinfo=timezone.utc
            )
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=clock,
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            shutil.copytree(root, clone)
            FinResearchOps(artifact_root=clone, clock=clock).handle(
                SubmitReview(
                    created.case_ref,
                    analysis.run_ref,
                    ReviewAction.APPROVE,
                    "Source for malformed intent.",
                )
            )
            clone_transactions = (
                clone
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            source_intent = next(
                path
                for path in clone_transactions.glob("*.intent.json")
                if json.loads(path.read_bytes())["sequence"] == 2
            )
            intent = json.loads(source_intent.read_bytes())
            intent["material_payload"]["action"] = "BOGUS"
            review_body = {
                key: value
                for key, value in intent["material_payload"].items()
                if key != "review_ref"
            }
            review_ref = "review-" + sha256_hex(
                canonical_json_bytes(review_body)
            )
            intent["material_ref"] = review_ref
            intent["material_payload"]["review_ref"] = review_ref
            intent["event"]["review_ref"] = review_ref
            intent["next_head"]["tail_event_sha256"] = sha256_hex(
                canonical_json_bytes(intent["event"])
            )
            intent_body = {
                key: value
                for key, value in intent.items()
                if key != "transaction_ref"
            }
            transaction_ref = "transaction-" + sha256_hex(
                canonical_json_bytes(intent_body)
            )
            intent["transaction_ref"] = transaction_ref
            transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            (transaction_directory / f"{transaction_ref}.intent.json").write_bytes(
                canonical_json_bytes(intent)
            )
            head_path = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "journal-head.json"
            )
            head_before = head_path.read_bytes()

            with self.assertRaises(ApplicationError) as error:
                FinResearchOps(artifact_root=root).read_case(created.case_ref)

            head_after = head_path.read_bytes()
            event_two_exists = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "events"
                / "00000002.json"
            ).exists()

        self.assertEqual("REVIEW_RECORD_VALUE_INVALID", error.exception.code)
        self.assertEqual(head_before, head_after)
        self.assertFalse(event_two_exists)

    def test_pending_analysis_cannot_rehome_a_run_from_another_case(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-aurora-revenue-growth-v2": (
                            aurora_candidate()
                        )
                    }
                ),
            )
            early = application.handle(
                CreateCase(
                    "What was Aurora Devices FY2025 revenue growth versus FY2024?",
                    date(2026, 2, 14),
                    aurora_document(),
                )
            )
            late = application.handle(
                CreateCase(
                    "What was Aurora Devices FY2025 revenue growth versus FY2024?",
                    date(2026, 3, 1),
                    aurora_document(),
                )
            )
            late_analysis = application.handle(RunAnalysis(late.case_ref))
            late_case_directory = (
                root / "application" / "cases" / late.case_ref
            )
            late_workpaper = json.loads(
                (
                    late_case_directory
                    / "workpapers"
                    / f"{late_analysis.workpaper_ref}.json"
                ).read_bytes()
            )
            late_workpaper["case_ref"] = early.case_ref
            workpaper_body = {
                key: value
                for key, value in late_workpaper.items()
                if key != "workpaper_ref"
            }
            foreign_ref = "workpaper-" + sha256_hex(
                canonical_json_bytes(workpaper_body)
            )
            late_workpaper["workpaper_ref"] = foreign_ref
            early_case_directory = (
                root / "application" / "cases" / early.case_ref
            )
            previous_head = json.loads(
                (early_case_directory / "journal-head.json").read_bytes()
            )
            event = {
                "schema_version": "finresearchops.case-event.analysis/v1",
                "run_ref": {"run_id": late_analysis.run_ref.run_id},
                "workpaper_ref": foreign_ref,
                "sequence": 1,
                "case_ref": early.case_ref,
                "previous_event_sha256": None,
            }
            next_head = {
                "schema_version": "finresearchops.case-journal-head/v1",
                "case_ref": early.case_ref,
                "event_count": 1,
                "tail_event_sha256": sha256_hex(
                    canonical_json_bytes(event)
                ),
            }
            intent_body = {
                "schema_version": "finresearchops.case-transaction/v1",
                "case_ref": early.case_ref,
                "sequence": 1,
                "previous_head": previous_head,
                "next_head": next_head,
                "command_identity": {
                    "schema_version": RunAnalysis.schema_version,
                    "case_ref": early.case_ref,
                },
                "material_kind": "workpaper",
                "material_ref": foreign_ref,
                "material_payload": late_workpaper,
                "event": event,
            }
            transaction_ref = "transaction-" + sha256_hex(
                canonical_json_bytes(intent_body)
            )
            intent = {**intent_body, "transaction_ref": transaction_ref}
            transaction_directory = early_case_directory / "transactions"
            transaction_directory.mkdir(parents=True, exist_ok=True)
            (transaction_directory / f"{transaction_ref}.intent.json").write_bytes(
                canonical_json_bytes(intent)
            )

            with self.assertRaises(ApplicationError) as error:
                application.handle(RunAnalysis(early.case_ref))
            head_after = (early_case_directory / "journal-head.json").read_bytes()
            event_exists = (early_case_directory / "events" / "00000001.json").exists()

        self.assertEqual("WORKPAPER_CASE_TASK_MISMATCH", error.exception.code)
        self.assertEqual(
            canonical_json_bytes(previous_head),
            head_after,
        )
        self.assertFalse(event_exists)

    def test_committed_transaction_rejects_forged_command_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            application.handle(RunAnalysis(created.case_ref))
            transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            original_intent_path = next(
                transaction_directory.glob("*.intent.json")
            )
            intent = json.loads(original_intent_path.read_bytes())
            original_ref = intent["transaction_ref"]
            original_commit_path = (
                transaction_directory / f"{original_ref}.commit.json"
            )
            intent["command_identity"] = {
                "schema_version": "forged.command/v999",
                "case_ref": created.case_ref,
            }
            intent_body = {
                key: value
                for key, value in intent.items()
                if key != "transaction_ref"
            }
            forged_ref = "transaction-" + sha256_hex(
                canonical_json_bytes(intent_body)
            )
            intent["transaction_ref"] = forged_ref
            commit = json.loads(original_commit_path.read_bytes())
            commit["transaction_ref"] = forged_ref
            original_intent_path.unlink()
            original_commit_path.unlink()
            (
                transaction_directory / f"{forged_ref}.intent.json"
            ).write_bytes(canonical_json_bytes(intent))
            (
                transaction_directory / f"{forged_ref}.commit.json"
            ).write_bytes(canonical_json_bytes(commit))

            with self.assertRaises(ApplicationError) as error:
                application.read_case(created.case_ref)

        self.assertEqual(
            "CASE_TRANSACTION_COMMAND_INVALID",
            error.exception.code,
        )

    def test_missing_non_tail_commit_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026, 8, 13, 13, 55, tzinfo=timezone.utc
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            application.handle(
                SubmitReview(
                    created.case_ref,
                    analysis.run_ref,
                    ReviewAction.APPROVE,
                    "Seal the second transition.",
                )
            )
            transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            first_intent = next(
                path
                for path in transaction_directory.glob("*.intent.json")
                if json.loads(path.read_bytes())["sequence"] == 1
            )
            first_ref = json.loads(first_intent.read_bytes())["transaction_ref"]
            (transaction_directory / f"{first_ref}.commit.json").unlink()

            with self.assertRaises(ApplicationError) as error:
                application.read_case(created.case_ref)

        self.assertEqual(
            "CASE_TRANSACTION_COMMIT_INVALID",
            error.exception.code,
        )

    def test_unsealed_tail_cannot_coexist_with_a_later_pending_intent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026, 8, 13, 14, 0, tzinfo=timezone.utc
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            original_write_once = application_module.write_once

            def fail_review(path: Path, payload: bytes) -> None:
                if path.parent.name == "reviews":
                    raise OSError("leave the next review pending")
                original_write_once(path, payload)

            with patch(
                "finauditgate.application.module.write_once",
                side_effect=fail_review,
            ):
                with self.assertRaises(ApplicationError):
                    application.handle(
                        SubmitReview(
                            created.case_ref,
                            analysis.run_ref,
                            ReviewAction.APPROVE,
                            "Leave a pending successor.",
                        )
                    )
            transaction_directory = (
                root
                / "application"
                / "cases"
                / created.case_ref
                / "transactions"
            )
            first_intent = next(
                path
                for path in transaction_directory.glob("*.intent.json")
                if json.loads(path.read_bytes())["sequence"] == 1
            )
            first_ref = json.loads(first_intent.read_bytes())["transaction_ref"]
            (transaction_directory / f"{first_ref}.commit.json").unlink()

            with self.assertRaises(ApplicationError) as error:
                application.read_case(created.case_ref)

        self.assertEqual(
            "CASE_TRANSACTION_COMMIT_INVALID",
            error.exception.code,
        )

    def test_resumed_non_accept_review_preserves_gate_reason_codes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-aurora-revenue-growth-v2": (
                            aurora_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026, 8, 13, 14, 5, tzinfo=timezone.utc
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was Aurora Devices FY2025 revenue growth versus FY2024?",
                    date(2026, 2, 14),
                    aurora_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            command = SubmitReview(
                created.case_ref,
                analysis.run_ref,
                ReviewAction.RETURN,
                "Return the post-cutoff result.",
            )
            original_write_once = application_module.write_once

            def fail_event(path: Path, payload: bytes) -> None:
                if path.parent.name == "events":
                    raise OSError("leave pending non-accept review")
                original_write_once(path, payload)

            with patch(
                "finauditgate.application.module.write_once",
                side_effect=fail_event,
            ):
                with self.assertRaises(ApplicationError):
                    application.handle(command)
            resumed = FinResearchOps(artifact_root=root).handle(command)

        self.assertEqual(Decision.HUMAN_REVIEW, resumed.machine_decision)
        self.assertEqual(
            ("POST_CUTOFF_DOCUMENT",),
            resumed.reason_codes,
        )

    def test_persistence_conflict_is_a_stable_application_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(artifact_root=root)
            command = CreateCase(
                "What was FY2025 revenue growth versus FY2024?",
                date(2026, 8, 12),
                northstar_document(),
            )
            created = application.handle(command)
            blob_path = (
                root
                / "application"
                / "blobs"
                / "sha256"
                / application.read_case(created.case_ref).source_ref.document_sha256
            )
            blob_path.write_bytes(b"forged")

            with self.assertRaises(ApplicationError) as error:
                application.handle(command)
            self.assertEqual(
                "APPLICATION_ARTIFACT_CONFLICT",
                error.exception.code,
            )

    def test_concurrent_analysis_rechecks_state_before_any_second_commit(
        self,
    ) -> None:
        class BlockingAdapter:
            def __init__(self) -> None:
                self.entered = Event()
                self.release = Event()

            def propose(
                self,
                task: object,
                attempt_index: int = 0,
            ) -> ScriptedCandidate:
                self.entered.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError("test did not release blocking Adapter")
                return northstar_candidate()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            blocker = BlockingAdapter()
            first_application = FinResearchOps(
                artifact_root=root,
                model=blocker,
            )
            second_application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = first_application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            second_finished = Event()

            def second_run() -> object:
                try:
                    return second_application.handle(
                        RunAnalysis(created.case_ref)
                    )
                finally:
                    second_finished.set()

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(
                    first_application.handle,
                    RunAnalysis(created.case_ref),
                )
                self.assertTrue(blocker.entered.wait(timeout=2))
                second_future = executor.submit(second_run)
                completed_before_release = second_finished.wait(timeout=0.2)
                blocker.release.set()
                first_result = first_future.result(timeout=5)
                try:
                    second_result: object = second_future.result(timeout=5)
                except ApplicationError as exc:
                    second_result = exc

            self.assertFalse(completed_before_release)
            self.assertEqual(Decision.ACCEPT, first_result.machine_decision)
            self.assertIsInstance(second_result, ApplicationError)
            self.assertEqual(
                "ANALYSIS_NOT_ALLOWED_IN_CURRENT_STATE",
                second_result.code,
            )
            view = first_application.read_case(created.case_ref)
            self.assertEqual(CaseStatus.AWAITING_REVIEW, view.status)
            self.assertEqual(1, len(view.run_refs))

    def test_concurrent_identical_replays_are_content_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            application = FinResearchOps(
                artifact_root=root,
                model=ScriptedModelAdapter(
                    {
                        "synthetic-northstar-revenue-v1": (
                            northstar_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            worker_count = 16
            barrier = Barrier(worker_count)

            def replay_once() -> object:
                worker = FinResearchOps(artifact_root=root)
                barrier.wait(timeout=5)
                return worker.handle(ReplayRun(analysis.run_ref))

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                outcomes = tuple(
                    executor.map(
                        lambda _: replay_once(),
                        range(worker_count),
                    )
                )

            self.assertEqual(
                1,
                len({outcome.replay_ref for outcome in outcomes}),
            )
            self.assertEqual(
                1,
                len(application.read_case(created.case_ref).replays),
            )

    def test_concurrent_identical_create_case_is_content_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = CreateCase(
                "What was FY2025 revenue growth versus FY2024?",
                date(2026, 8, 12),
                northstar_document(),
            )
            worker_count = 16
            barrier = Barrier(worker_count)

            def create_once() -> object:
                worker = FinResearchOps(artifact_root=root)
                barrier.wait(timeout=5)
                return worker.handle(command)

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                outcomes = tuple(
                    executor.map(
                        lambda _: create_once(),
                        range(worker_count),
                    )
                )

            self.assertEqual(
                1,
                len({outcome.case_ref for outcome in outcomes}),
            )
            application = FinResearchOps(artifact_root=root)
            view = application.read_case(outcomes[0].case_ref)
            self.assertEqual(CaseStatus.CREATED, view.status)

    def test_different_cases_can_atomically_share_one_document_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            commands = (
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 12),
                    northstar_document(),
                ),
                CreateCase(
                    "What was FY2025 revenue growth versus FY2024?",
                    date(2026, 8, 13),
                    northstar_document(),
                ),
            )
            outcomes: tuple[object, ...] = ()
            round_root = root
            for round_index in range(64):
                round_root = root / f"round-{round_index:03d}"
                barrier = Barrier(2)

                def create_once(command: CreateCase) -> object:
                    worker = FinResearchOps(artifact_root=round_root)
                    barrier.wait(timeout=5)
                    return worker.handle(command)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = tuple(executor.map(create_once, commands))
                self.assertEqual(2, len({item.case_ref for item in outcomes}))

            application = FinResearchOps(artifact_root=round_root)
            for outcome in outcomes:
                self.assertEqual(
                    CaseStatus.CREATED,
                    application.read_case(outcome.case_ref).status,
                )

    def test_m2_scripted_journey_preserves_normalized_lineage_to_packet(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application = FinResearchOps(
                artifact_root=Path(temporary_directory),
                model=ScriptedModelAdapter(
                    {
                        "synthetic-aurora-revenue-growth-v2": (
                            aurora_candidate()
                        )
                    }
                ),
                clock=lambda: datetime(
                    2026,
                    8,
                    13,
                    12,
                    4,
                    tzinfo=timezone.utc,
                ),
            )
            created = application.handle(
                CreateCase(
                    question=(
                        "What was Aurora Devices FY2025 revenue growth "
                        "versus FY2024?"
                    ),
                    cutoff=date(2026, 3, 1),
                    document=aurora_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            application.handle(
                SubmitReview(
                    created.case_ref,
                    analysis.run_ref,
                    ReviewAction.APPROVE,
                    "Registered semantics and lineage checked.",
                )
            )
            application.handle(
                ExportChangePacket(created.case_ref, analysis.run_ref)
            )
            application.handle(ReplayRun(analysis.run_ref))
            view = application.read_case(created.case_ref)

            self.assertEqual(CaseStatus.APPROVED, view.status)
            self.assertIsNotNone(view.workpapers[0].attempts_artifact_ref)
            self.assertEqual(
                view.workpapers[0].attempts_artifact_ref,
                view.workpapers[0].trace_summary_ref,
            )
            self.assertEqual(
                "ATTEMPTS_RECORDED",
                view.workpapers[0].trace_summary_status,
            )
            self.assertEqual("20.00", view.workpapers[0].answer)
            self.assertEqual(2, len(view.packets[0].verified_facts))
            current = next(
                fact
                for fact in view.packets[0].verified_facts
                if fact.evidence_id == "revenue_current"
            )
            self.assertEqual("revenue", current.metric)
            self.assertEqual("REPORTED", current.metric_basis)
            self.assertEqual("FY2025", current.period)
            self.assertEqual("USD", current.currency)
            self.assertEqual("MONETARY", current.unit)
            self.assertEqual("MILLION", current.scale)
            self.assertEqual("POSITIVE", current.sign)
            self.assertEqual(64, len(current.span_sha256))
            self.assertEqual(
                ("revenue_current", "revenue_prior"),
                view.packets[0].calculations[0].operand_ids,
            )
            self.assertEqual(Decision.ACCEPT, view.replays[0].report.decision)

    def test_m2_human_review_reason_reaches_pending_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application = FinResearchOps(
                artifact_root=Path(temporary_directory),
                model=ScriptedModelAdapter(
                    {
                        "synthetic-aurora-revenue-growth-v2": (
                            aurora_candidate()
                        )
                    }
                ),
            )
            created = application.handle(
                CreateCase(
                    question=(
                        "What was Aurora Devices FY2025 revenue growth "
                        "versus FY2024?"
                    ),
                    cutoff=date(2026, 2, 14),
                    document=aurora_document(),
                )
            )
            analysis = application.handle(RunAnalysis(created.case_ref))
            view = application.read_case(created.case_ref)

            self.assertEqual(Decision.HUMAN_REVIEW, analysis.machine_decision)
            self.assertEqual(("POST_CUTOFF_DOCUMENT",), analysis.reason_codes)
            self.assertEqual(CaseStatus.AWAITING_REVIEW, view.status)
            self.assertEqual(("POST_CUTOFF_DOCUMENT",), view.latest_gate_reason_codes)
            self.assertIsNone(view.workpapers[0].answer)
            with self.assertRaises(ApplicationError):
                application.handle(
                    SubmitReview(
                        created.case_ref,
                        analysis.run_ref,
                        ReviewAction.APPROVE,
                        "A person cannot override the cutoff gate.",
                    )
                )


if __name__ == "__main__":
    unittest.main()
