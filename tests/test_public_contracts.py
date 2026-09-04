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

import finauditgate
from finauditgate import (
    AuditOutcome,
    AuditTask,
    Decision,
    FinAuditGate,
    FrozenDocumentPackage,
    ReplayReport,
    RunRef,
)
from finauditgate.adapters.scripted import ScriptedModelAdapter


SCHEMA_DIR = Path(__file__).parents[1] / "schemas"
FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "synthetic"
    / "aurora_revenue_growth_m2.txt"
)
DEMO_SCRIPT = Path(__file__).parents[1] / "scripts" / "synthetic_demo.py"


class GenerationBudgetTest(unittest.TestCase):
    def test_the_prompt_and_the_answer_both_fit_the_generation_context(
        self,
    ) -> None:
        """Overflowing the context is silent, so it must be impossible.

        The runtime does not refuse a prompt longer than the context: it
        discards the front of it, which is where the system message and the
        schema are, and answers anyway. Such a run completes and replays, so
        nothing downstream can notice. These three constants are what prevent
        it, and they are only safe together.
        """

        from finauditgate.adapters import ollama_route as route

        worst_case = (
            route.SYSTEM_PROMPT_TOKEN_ALLOWANCE
            + route.MAX_DOCUMENT_BYTES // route.DOCUMENT_BYTES_PER_TOKEN
            + route.GENERATION_BUDGET
        )

        self.assertLessEqual(worst_case, route.GENERATION_CONTEXT)
        # The allowance has to cover the system message the route actually
        # sends, measured the same way the budget assumes.
        system_bytes = len(route.system_message().encode("utf-8"))
        self.assertLessEqual(
            system_bytes // route.DOCUMENT_BYTES_PER_TOKEN,
            route.SYSTEM_PROMPT_TOKEN_ALLOWANCE,
        )


class PublicContractTest(unittest.TestCase):
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
        self.assertEqual(("self", "task"), tuple(signature(FinAuditGate.run).parameters))
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
            gate = FinAuditGate(artifact_root=artifact_root, model=ScriptedModelAdapter({}))

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
            gate = FinAuditGate(artifact_root=artifact_root, model=ScriptedModelAdapter({}))

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
            with self.assertRaisesRegex(TypeError, "run_ref must be a RunRef"):
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
                    source_id="synthetic-aurora-revenue-growth-v2",
                    document_name="aurora_revenue_growth_m2.txt",
                    document_bytes=FIXTURE_PATH.read_bytes(),
                    declared_published_at=date(2026, 8, 12),
                ),
            )
        with self.assertRaisesRegex(TypeError, "source_id must be a string"):
            FrozenDocumentPackage(
                source_id=EquivocatingStr("spoofed-source"),
                document_name="aurora_revenue_growth_m2.txt",
                document_bytes=FIXTURE_PATH.read_bytes(),
                declared_published_at=date(2026, 8, 12),
            )
        with self.assertRaisesRegex(
            ValueError,
            "run_id must be a lowercase SHA-256 hex digest",
        ):
            RunRef(run_id=EquivocatingStr("0" * 64))

    def test_schema_files_freeze_the_public_contracts(self) -> None:
        expected = {
            "run-task-artifact.v1.schema.json": (
                "urn:finauditgate:schema:run-task-artifact:v1",
                "finauditgate.task/v1",
            ),
            "run-ref.v1.schema.json": ("urn:finauditgate:schema:run-ref:v1", None),
            "audit-outcome.v1.schema.json": (
                "urn:finauditgate:schema:audit-outcome:v1",
                "finauditgate.run/v1",
            ),
            "replay-report.v5.schema.json": (
                "urn:finauditgate:schema:replay-report:v5",
                "finauditgate.replay/v5",
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
                self.assertEqual(set(payload["required"]), set(payload["properties"]))
                if schema_version is not None:
                    self.assertEqual(
                        schema_version,
                        payload["properties"]["schema_version"]["const"],
                    )
        self.assertEqual(
            sorted(path.name for path in SCHEMA_DIR.glob("*.json")),
            [
                "audit-outcome.v1.schema.json",
                "case-journal-head.v1.schema.json",
                "case-record.v1.schema.json",
                "case-transaction-commit.v1.schema.json",
                "case-transaction.v1.schema.json",
                "replay-record.v3.schema.json",
                "replay-report.v5.schema.json",
                "research-change-packet.v1.schema.json",
                "review-record.v1.schema.json",
                "run-ref.v1.schema.json",
                "run-task-artifact.v1.schema.json",
                "workpaper.v3.schema.json",
            ],
        )

    def test_public_synthetic_demo_runs_and_replays_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory) / "artifacts"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DEMO_SCRIPT),
                    "--artifact-root",
                    str(artifact_root),
                ],
                cwd=Path(__file__).parents[1],
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                },
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
        self.assertEqual("finauditgate.replay/v5", payload["replay"]["schema_version"])
        self.assertEqual(9, payload["replay"]["verified_artifact_count"])
        self.assertEqual(payload["outcome"]["run_ref"], payload["replay"]["run_ref"])


if __name__ == "__main__":
    unittest.main()
