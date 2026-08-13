from dataclasses import fields
from datetime import date
from inspect import signature
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import finauditgate
import finauditgate.core.engine as engine
from finauditgate import (
    AuditOutcome,
    AuditTask,
    Decision,
    FinAuditGate,
    FrozenDocumentPackage,
    ReplayReport,
    RunRef,
)
from finauditgate.adapters.scripted import (
    CalculationCandidate,
    EvidenceCandidate,
    ScriptedCandidate,
    ScriptedModelAdapter,
)


SCHEMA_DIR = Path(__file__).parents[1] / "schemas"
FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "northstar_revenue.txt"
)
JOURNEY_PATH = SCHEMA_DIR / "examples" / "synthetic-product-journey.draft.v1.json"
DEMO_SCRIPT = Path(__file__).parents[1] / "scripts" / "synthetic_demo.py"


class M1ContractTest(unittest.TestCase):
    def test_public_core_interface_is_frozen_at_root_exports(self) -> None:
        self.assertEqual(
            (
                "AuditOutcome",
                "AuditTask",
                "Decision",
                "FinAuditGate",
                "FrozenDocumentPackage",
                "ReplayReport",
                "RunRef",
            ),
            finauditgate.__all__,
        )
        self.assertEqual(
            (
                "task_id",
                "question",
                "cutoff",
                "document",
                "answer_contract",
                "risk_class",
                "mode",
            ),
            tuple(field.name for field in fields(AuditTask)),
        )
        self.assertEqual(
            (
                "source_id",
                "document_name",
                "document_bytes",
                "declared_published_at",
            ),
            tuple(field.name for field in fields(FrozenDocumentPackage)),
        )
        self.assertEqual(
            (
                "schema_version",
                "task_id",
                "decision",
                "answer",
                "answer_unit",
                "run_ref",
                "document_sha256",
                "reason_codes",
            ),
            tuple(field.name for field in fields(AuditOutcome)),
        )
        self.assertEqual(("run_id",), tuple(field.name for field in fields(RunRef)))
        self.assertEqual(
            (
                "schema_version",
                "run_ref",
                "consistent",
                "decision",
                "answer",
                "answer_unit",
                "verified_artifact_count",
                "reason",
            ),
            tuple(field.name for field in fields(ReplayReport)),
        )
        self.assertEqual(
            ("ACCEPT", "RETRY", "ABSTAIN", "HUMAN_REVIEW"),
            tuple(decision.value for decision in Decision),
        )
        self.assertEqual(
            ("self", "task"),
            tuple(signature(FinAuditGate.run).parameters),
        )
        self.assertEqual(
            ("self", "run_ref"),
            tuple(signature(FinAuditGate.replay).parameters),
        )

    def test_candidate_adapter_failure_closes_before_artifacts_exist(self) -> None:
        task = AuditTask(
            task_id="missing-scripted-candidate",
            question="What was revenue growth?",
            cutoff=date(2026, 8, 12),
            document=FrozenDocumentPackage(
                source_id="synthetic-missing-candidate",
                document_name="synthetic.txt",
                document_bytes=b"synthetic bytes",
                declared_published_at=date(2026, 8, 12),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory) / "artifacts"
            gate = FinAuditGate(
                artifact_root=artifact_root,
                model=ScriptedModelAdapter({}),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "candidate model Adapter failed before a run could be committed",
            ):
                gate.run(task)

            self.assertFalse(artifact_root.exists())

    def test_non_contract_task_fails_before_adapter_or_artifacts(self) -> None:
        class StatefulTask:
            task_id = "stateful-task"
            question = "What was FY2025 revenue growth versus FY2024?"

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory) / "artifacts"
            gate = FinAuditGate(
                artifact_root=artifact_root,
                model=ScriptedModelAdapter({}),
            )

            with self.assertRaisesRegex(TypeError, "task must be an AuditTask"):
                gate.run(StatefulTask())  # type: ignore[arg-type]

            self.assertFalse(artifact_root.exists())

    def test_run_ref_rejects_an_invalid_content_identity(self) -> None:
        for invalid_run_id in (None, "", "not-a-sha256", "A" * 64):
            with self.subTest(invalid_run_id=invalid_run_id):
                with self.assertRaisesRegex(
                    ValueError,
                    "run_id must be a lowercase SHA-256 hex digest",
                ):
                    RunRef(run_id=invalid_run_id)  # type: ignore[arg-type]

    def test_replay_rejects_a_structurally_similar_fake_run_ref(self) -> None:
        class FakeRunRef:
            run_id = "0" * 64

        with tempfile.TemporaryDirectory() as temporary_directory:
            gate = FinAuditGate(artifact_root=Path(temporary_directory))
            with self.assertRaisesRegex(
                TypeError,
                "run_ref must be a RunRef",
            ):
                gate.replay(FakeRunRef())  # type: ignore[arg-type]

    def test_audit_task_requires_immutable_document_bytes(self) -> None:
        for mutable_document in (bytearray(b"bytes"), memoryview(b"bytes")):
            with self.subTest(document_type=type(mutable_document).__name__):
                with self.assertRaisesRegex(
                    TypeError,
                    "document_bytes must be immutable bytes",
                ):
                    FrozenDocumentPackage(
                        source_id="synthetic-immutable-contract",
                        document_name="synthetic.txt",
                        document_bytes=mutable_document,  # type: ignore[arg-type]
                        declared_published_at=date(2026, 8, 12),
                    )

    def test_public_contracts_reject_string_subclasses(self) -> None:
        class EquivocatingStr(str):
            def __eq__(self, other: object) -> bool:
                return True

            __hash__ = str.__hash__

        with self.assertRaisesRegex(TypeError, "question must be a string"):
            AuditTask(
                task_id="synthetic-string-subclass",
                question=EquivocatingStr("What was profit growth?"),
                cutoff=date(2026, 8, 12),
                document=FrozenDocumentPackage(
                    source_id="synthetic-northstar-revenue-v1",
                    document_name="northstar_revenue.txt",
                    document_bytes=FIXTURE_PATH.read_bytes(),
                    declared_published_at=date(2026, 8, 12),
                ),
            )
        with self.assertRaisesRegex(TypeError, "source_id must be a string"):
            FrozenDocumentPackage(
                source_id=EquivocatingStr("spoofed-source"),
                document_name="northstar_revenue.txt",
                document_bytes=FIXTURE_PATH.read_bytes(),
                declared_published_at=date(2026, 8, 12),
            )
        with self.assertRaisesRegex(
            ValueError,
            "run_id must be a lowercase SHA-256 hex digest",
        ):
            RunRef(run_id=EquivocatingStr("0" * 64))

    def test_core_schema_files_freeze_the_public_contracts(self) -> None:
        expected = {
            "run-task-artifact.v1.schema.json": {
                "id": "urn:finauditgate:schema:run-task-artifact:v1",
                "schema_version": "finauditgate.task/v1",
                "required": {
                    "schema_version",
                    "task_id",
                    "question",
                    "cutoff",
                    "source_id",
                    "document_name",
                    "document_sha256",
                    "declared_published_at",
                    "answer_contract",
                    "risk_class",
                    "mode",
                },
            },
            "run-ref.v1.schema.json": {
                "id": "urn:finauditgate:schema:run-ref:v1",
                "schema_version": None,
                "required": {"run_id"},
            },
            "audit-outcome.v1.schema.json": {
                "id": "urn:finauditgate:schema:audit-outcome:v1",
                "schema_version": "finauditgate.run/v1",
                "required": {
                    "schema_version",
                    "task_id",
                    "decision",
                    "answer",
                    "answer_unit",
                    "run_ref",
                    "document_sha256",
                    "reason_codes",
                },
            },
            "replay-report.v1.schema.json": {
                "id": "urn:finauditgate:schema:replay-report:v1",
                "schema_version": "finauditgate.replay/v1",
                "required": {
                    "schema_version",
                    "run_ref",
                    "consistent",
                    "decision",
                    "answer",
                    "answer_unit",
                    "verified_artifact_count",
                    "reason",
                },
            },
        }

        for filename, contract in expected.items():
            with self.subTest(filename=filename):
                payload = json.loads((SCHEMA_DIR / filename).read_bytes())
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema",
                    payload["$schema"],
                )
                self.assertEqual(contract["id"], payload["$id"])
                if filename == "run-task-artifact.v1.schema.json":
                    self.assertIn("Persisted", payload["title"])
                    self.assertIn("AuditTask", payload["description"])
                self.assertEqual("object", payload["type"])
                self.assertFalse(payload["additionalProperties"])
                self.assertEqual(contract["required"], set(payload["required"]))
                if contract["schema_version"] is not None:
                    self.assertEqual(
                        contract["schema_version"],
                        payload["properties"]["schema_version"]["const"],
                    )

    def test_product_schema_drafts_keep_machine_and_human_states_separate(self) -> None:
        expected = {
            "case-record.draft.v1.schema.json": {
                "id": "urn:finresearchops:schema:case-record:draft:v1",
                "schema_version": "finresearchops.case-record/draft-v1",
                "required": {
                    "schema_version",
                    "case_ref",
                    "question",
                    "cutoff",
                    "source_ref",
                    "status",
                    "run_refs",
                },
            },
            "workpaper.draft.v1.schema.json": {
                "id": "urn:finresearchops:schema:workpaper:draft:v1",
                "schema_version": "finresearchops.workpaper/draft-v1",
                "required": {
                    "schema_version",
                    "workpaper_ref",
                    "case_ref",
                    "run_ref",
                    "machine_decision",
                    "answer",
                    "answer_unit",
                    "document_sha256",
                    "evidence_ledger_ref",
                    "formula_ref",
                    "gate_reason_codes",
                    "candidate_artifact_ref",
                    "trace_summary_ref",
                    "trace_summary_status",
                },
            },
            "review-record.draft.v1.schema.json": {
                "id": "urn:finresearchops:schema:review-record:draft:v1",
                "schema_version": "finresearchops.review-record/draft-v1",
                "required": {
                    "schema_version",
                    "review_ref",
                    "case_ref",
                    "run_ref",
                    "workpaper_ref",
                    "action",
                    "reason",
                    "recorded_at",
                    "previous_case_status",
                    "resulting_case_status",
                },
            },
            "research-change-packet.draft.v1.schema.json": {
                "id": "urn:finresearchops:schema:research-change-packet:draft:v1",
                "schema_version": "finresearchops.research-change-packet/draft-v1",
                "required": {
                    "schema_version",
                    "packet_ref",
                    "case_ref",
                    "run_ref",
                    "review_ref",
                    "workpaper_ref",
                    "proposal_only",
                    "verified_facts",
                    "calculations",
                    "impact_statement",
                    "unresolved_items",
                },
            },
        }

        machine_decisions = {decision.value for decision in Decision}
        human_actions = {"APPROVE", "RETURN", "REJECT"}
        self.assertTrue(machine_decisions.isdisjoint(human_actions))

        for filename, contract in expected.items():
            with self.subTest(filename=filename):
                payload = json.loads((SCHEMA_DIR / filename).read_bytes())
                self.assertEqual(contract["id"], payload["$id"])
                self.assertEqual("object", payload["type"])
                self.assertFalse(payload["additionalProperties"])
                self.assertEqual(contract["required"], set(payload["required"]))
                self.assertEqual(
                    contract["schema_version"],
                    payload["properties"]["schema_version"]["const"],
                )

        review = json.loads(
            (SCHEMA_DIR / "review-record.draft.v1.schema.json").read_bytes()
        )
        self.assertEqual(human_actions, set(review["properties"]["action"]["enum"]))
        self.assertNotIn("machine_decision", review["properties"])
        action_transitions = {
            branch["if"]["properties"]["action"]["const"]:
                branch["then"]["properties"]["resulting_case_status"]["const"]
            for branch in review["allOf"]
        }
        self.assertEqual(
            {
                "APPROVE": "APPROVED",
                "RETURN": "RETURNED",
                "REJECT": "REJECTED",
            },
            action_transitions,
        )

        workpaper = json.loads(
            (SCHEMA_DIR / "workpaper.draft.v1.schema.json").read_bytes()
        )
        self.assertEqual(
            machine_decisions,
            set(workpaper["properties"]["machine_decision"]["enum"]),
        )
        self.assertNotIn("human_action", workpaper["properties"])
        workpaper_decision_rule = workpaper["allOf"][0]
        self.assertEqual(
            "ACCEPT",
            workpaper_decision_rule["if"]["properties"][
                "machine_decision"
            ]["const"],
        )
        self.assertEqual(
            0,
            workpaper_decision_rule["then"]["properties"][
                "gate_reason_codes"
            ]["maxItems"],
        )
        self.assertEqual(
            1,
            workpaper_decision_rule["else"]["properties"][
                "gate_reason_codes"
            ]["minItems"],
        )

        case_record = json.loads(
            (SCHEMA_DIR / "case-record.draft.v1.schema.json").read_bytes()
        )
        self.assertEqual(
            {
                "source_id",
                "document_name",
                "document_sha256",
                "declared_published_at",
            },
            set(case_record["properties"]["source_ref"]["required"]),
        )
        case_run_rule = case_record["allOf"][0]
        self.assertEqual(
            0,
            case_run_rule["then"]["properties"]["run_refs"]["maxItems"],
        )
        self.assertEqual(
            1,
            case_run_rule["else"]["properties"]["run_refs"]["minItems"],
        )

        packet = json.loads(
            (
                SCHEMA_DIR
                / "research-change-packet.draft.v1.schema.json"
            ).read_bytes()
        )
        self.assertTrue(packet["properties"]["proposal_only"]["const"])
        self.assertNotIn("formal_research_state", packet["properties"])

    def test_synthetic_journey_maps_one_core_run_without_application_logic(self) -> None:
        journey = json.loads(JOURNEY_PATH.read_bytes())
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )

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
        candidate = ScriptedCandidate(
            evidence=(
                evidence(
                    "revenue_prior",
                    prior_record,
                    "FY2024",
                    "100.00",
                ),
                evidence(
                    "revenue_current",
                    current_record,
                    "FY2025",
                    "120.00",
                ),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=artifact_root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            replay = FinAuditGate(artifact_root=artifact_root).replay(
                outcome.run_ref
            )

        expected_run_ref = {"run_id": outcome.run_ref.run_id}
        self.assertEqual(
            "d50255f31de3b81c5ac3f28c84b6e19d5bd06396bd2cb2d79547b46b19c4886f",
            outcome.run_ref.run_id,
        )
        self.assertTrue(journey["contract_example_only"])
        self.assertEqual(
            "NOT_STARTED",
            journey["application_implementation_status"],
        )
        self.assertEqual(expected_run_ref, journey["audit_outcome"]["run_ref"])
        self.assertEqual(expected_run_ref, journey["workpaper"]["run_ref"])
        self.assertEqual(expected_run_ref, journey["review_record"]["run_ref"])
        self.assertEqual(expected_run_ref, journey["change_packet"]["run_ref"])
        self.assertEqual(expected_run_ref, journey["replay_report"]["run_ref"])
        self.assertEqual(
            [expected_run_ref],
            journey["case_record"]["run_refs"],
        )
        self.assertEqual(
            outcome.document_sha256,
            journey["case_record"]["source_ref"]["document_sha256"],
        )
        self.assertEqual(
            outcome.document_sha256,
            journey["workpaper"]["document_sha256"],
        )
        self.assertEqual(outcome.decision.value, journey["audit_outcome"]["decision"])
        self.assertEqual(outcome.decision.value, journey["workpaper"]["machine_decision"])
        self.assertEqual(outcome.answer, journey["audit_outcome"]["answer"])
        self.assertEqual(outcome.answer, journey["workpaper"]["answer"])
        self.assertEqual(outcome.answer_unit, journey["audit_outcome"]["answer_unit"])
        self.assertEqual(outcome.answer_unit, journey["workpaper"]["answer_unit"])
        self.assertEqual(replay.answer_unit, journey["replay_report"]["answer_unit"])
        self.assertEqual(
            journey["review_record"]["review_ref"],
            journey["change_packet"]["review_ref"],
        )
        self.assertEqual(
            journey["workpaper"]["workpaper_ref"],
            journey["review_record"]["workpaper_ref"],
        )
        self.assertEqual(
            journey["workpaper"]["workpaper_ref"],
            journey["change_packet"]["workpaper_ref"],
        )
        self.assertTrue(journey["change_packet"]["proposal_only"])
        run_prefix = f"runs/{outcome.run_ref.run_id}/"
        self.assertEqual(
            run_prefix + "ledger.json",
            journey["workpaper"]["evidence_ledger_ref"],
        )
        self.assertEqual(
            run_prefix + "formula.json",
            journey["workpaper"]["formula_ref"],
        )
        self.assertEqual(
            run_prefix + "candidate.json",
            journey["workpaper"]["candidate_artifact_ref"],
        )
        self.assertIsNone(journey["workpaper"]["trace_summary_ref"])
        self.assertEqual(
            "NOT_STARTED",
            journey["workpaper"]["trace_summary_status"],
        )

    def test_public_synthetic_demo_runs_and_replays_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory) / "artifacts"
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DEMO_SCRIPT),
                    "--artifact-root",
                    str(artifact_root),
                ],
                cwd=Path(__file__).parents[1],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

        payload = json.loads(completed.stdout)
        self.assertEqual("finauditgate.synthetic-demo/v1", payload["schema_version"])
        self.assertEqual("ACCEPT", payload["outcome"]["decision"])
        self.assertEqual("20.00", payload["outcome"]["answer"])
        self.assertEqual("PERCENT", payload["outcome"]["answer_unit"])
        self.assertTrue(payload["replay"]["consistent"])
        self.assertEqual("ACCEPT", payload["replay"]["decision"])
        self.assertEqual("20.00", payload["replay"]["answer"])
        self.assertEqual("PERCENT", payload["replay"]["answer_unit"])
        self.assertEqual(8, payload["replay"]["verified_artifact_count"])
        self.assertEqual(
            payload["outcome"]["run_ref"],
            payload["replay"]["run_ref"],
        )

    def test_m1_run_replays_when_the_current_policy_pointer_advances(self) -> None:
        document = FIXTURE_PATH.read_bytes()
        prior_record = (
            b"metric=revenue;period=FY2024;value=100.00;unit=USD_MILLION"
        )
        current_record = (
            b"metric=revenue;period=FY2025;value=120.00;unit=USD_MILLION"
        )
        task = AuditTask(
            task_id="synthetic-policy-compatibility",
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
                EvidenceCandidate(
                    evidence_id="revenue_prior",
                    byte_start=document.index(prior_record),
                    byte_end=document.index(prior_record) + len(prior_record),
                    metric="revenue",
                    period="FY2024",
                    value="100.00",
                    unit="USD_MILLION",
                ),
                EvidenceCandidate(
                    evidence_id="revenue_current",
                    byte_start=document.index(current_record),
                    byte_end=(
                        document.index(current_record) + len(current_record)
                    ),
                    metric="revenue",
                    period="FY2025",
                    value="120.00",
                    unit="USD_MILLION",
                ),
            ),
            calculation=CalculationCandidate(
                operation="growth_rate_percent",
                operand_ids=("revenue_current", "revenue_prior"),
                output_unit="PERCENT",
                quantize="0.01",
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory)
            outcome = FinAuditGate(
                artifact_root=artifact_root,
                model=ScriptedModelAdapter({task.task_id: candidate}),
            ).run(task)
            with patch.object(
                engine,
                "_CURRENT_CALCULATION_POLICY_SHA256",
                "0" * 64,
            ):
                replay = FinAuditGate(artifact_root=artifact_root).replay(
                    outcome.run_ref
                )

        self.assertTrue(replay.consistent)
        self.assertEqual(Decision.ACCEPT, replay.decision)
        self.assertEqual("20.00", replay.answer)


if __name__ == "__main__":
    unittest.main()
